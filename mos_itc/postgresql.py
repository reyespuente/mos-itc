"""Default local PostgreSQL cluster; per-database roles, never server superusers."""

import hashlib
import json
import secrets

from .core import CONF, STATE, Error, name, stamp

ROLES = {"read": "SELECT", "write": "SELECT, INSERT, UPDATE, DELETE", "admin": "ALL"}


def sql(system, statement, mutate=False, database="postgres"):
    try:
        return system.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-X",
                "-q",
                "-A",
                "-t",
                "-F",
                "\t",
                "--set",
                "ON_ERROR_STOP=1",
                "--dbname",
                database,
            ],
            input=statement,
            mutate=mutate,
        )
    except Error:
        raise Error("db_command_failed") from None


def managed(system):
    return system.data().get("postgresql_databases", {})


def require_database(system, database):
    database = name(database)
    if database not in managed(system) or database in (
        "postgres",
        "template0",
        "template1",
    ):
        raise Error("db_unmanaged")
    return database


def groups(database):
    prefix = "mos_" + hashlib.sha256(database.encode()).hexdigest()[:16]
    return {role: prefix + "_" + role for role in ROLES}


def inventory(system):
    output = sql(
        system,
        "SELECT datname, pg_database_size(oid) FROM pg_database "
        "WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname;",
    )
    ownership = managed(system)
    return [
        {
            "database": db,
            "tables": None,
            "bytes": int(size),
            "managed": db in ownership,
            "site": "",
        }
        for line in output.splitlines()
        if len(parts := line.split("\t")) == 2
        for db, size in [parts]
    ]


def create(system, database):
    database = name(database)
    if database in ("postgres", "template0", "template1") or database in {
        r["database"] for r in inventory(system)
    }:
        raise Error("db_exists")
    roles = groups(database)
    if sql(
        system,
        "SELECT rolname FROM pg_roles WHERE rolname IN ("
        + ",".join("'" + v + "'" for v in roles.values())
        + ");",
    ).strip():
        raise Error("db_user_exists")
    sql(
        system,
        "BEGIN;"
        + "".join(f'CREATE ROLE "{v}" NOLOGIN;' for v in roles.values())
        + "COMMIT;",
        True,
    )
    created = False
    try:
        sql(system, f'CREATE DATABASE "{database}" OWNER "{roles["admin"]}";', True)
        created = True
        sql(
            system,
            f"""BEGIN;
REVOKE ALL ON DATABASE "{database}" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO "{roles['admin']}";
GRANT CONNECT ON DATABASE "{database}" TO "{roles['read']}", "{roles['write']}";
GRANT USAGE ON SCHEMA public TO "{roles['read']}", "{roles['write']}";
ALTER DEFAULT PRIVILEGES FOR ROLE "{roles['admin']}" IN SCHEMA public GRANT SELECT ON TABLES TO "{roles['read']}";
ALTER DEFAULT PRIVILEGES FOR ROLE "{roles['admin']}" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{roles['write']}";
ALTER DEFAULT PRIVILEGES FOR ROLE "{roles['admin']}" IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO "{roles['write']}";
ALTER DEFAULT PRIVILEGES FOR ROLE "{roles['admin']}" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
COMMIT;""",
            True,
            database,
        )
        data = system.data()
        data.setdefault("postgresql_databases", {})[database] = {}
        system.save(data)
    except BaseException:
        if created:
            sql(system, f'DROP DATABASE "{database}";', True)
        sql(
            system,
            "DROP ROLE " + ",".join('"' + v + '"' for v in roles.values()) + ";",
            True,
        )
        raise
    system.audit("PostgreSQL database created: " + database)


def tables(system, database):
    database = require_database(system, database)
    return sql(
        system,
        "SELECT schemaname||'.'||relname, n_live_tup, "
        "pg_total_relation_size(relid) FROM pg_stat_user_tables ORDER BY schemaname, relname;",
        database=database,
    )


def accounts(system, database):
    require_database(system, database)
    return [
        {"username": name(user), "role": info["grants"][database], "primary": False}
        for user, info in system.data().get("postgresql_accounts", {}).items()
        if database in info.get("grants", {})
    ]


def credentials_path(system, database, username):
    if not any(a["username"] == username for a in accounts(system, database)):
        raise Error("db_unmanaged")
    return CONF / "database-users" / "postgresql" / (name(username) + ".json")


def credentials(system, database, username):
    try:
        value = json.loads(system.read(credentials_path(system, database, username)))
        if value["username"] != username or value["host"] != "127.0.0.1":
            raise ValueError
        return dict(value, database=database)
    except (ValueError, KeyError, TypeError):
        raise Error("db_credentials_missing") from None


def permission_sql(database, username, role):
    roles = groups(database)
    statements = [f'REVOKE "{group}" FROM "{username}";' for group in roles.values()]
    statements += [f'ALTER ROLE "{username}" IN DATABASE "{database}" RESET role;']
    if role:
        statements += [f'GRANT "{roles[role]}" TO "{username}";']
        if role == "admin":
            # New objects belong to the database owner; default grants then cover
            # future tables/sequences for readers and writers in public.
            statements += [
                f'ALTER ROLE "{username}" IN DATABASE "{database}" SET role TO \'{roles[role]}\';'
            ]
    return "".join(statements)


def grant(system, database, username, role):
    database = require_database(system, database)
    username = name(username)
    data = system.data()
    if role not in ROLES or username not in data.get("postgresql_accounts", {}):
        raise Error("db_invalid_role")
    data["postgresql_accounts"][username]["grants"][database] = role
    with system.transaction([STATE / "state.json"]):
        system.save(data)
        sql(
            system,
            "BEGIN;" + permission_sql(database, username, role) + "COMMIT;",
            True,
        )
    system.audit(f"PostgreSQL permission changed: {database} {username} {role}")


def create_user(system, database, username, role):
    database = require_database(system, database)
    username = name(username)
    if role not in ROLES:
        raise Error("db_invalid_role")
    if username.startswith(("pg_", "mos_")) or username == "postgres":
        raise Error("db_user_exists")
    if sql(system, f"SELECT rolname FROM pg_roles WHERE rolname='{username}';").strip():
        raise Error("db_user_exists")
    password = secrets.token_urlsafe(32)
    path = CONF / "database-users" / "postgresql" / (username + ".json")
    data = system.data()
    if (
        username in data.get("postgresql_accounts", {})
        or system.exists(path)
        or path.is_symlink()
    ):
        raise Error("db_user_exists")
    data.setdefault("postgresql_accounts", {})[username] = {"grants": {database: role}}
    with system.transaction([path, STATE / "state.json"]):
        system.write(
            path,
            json.dumps(
                {"username": username, "password": password, "host": "127.0.0.1"}
            ),
        )
        system.save(data)
        sql(
            system,
            f"BEGIN; CREATE ROLE \"{username}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}';"
            + permission_sql(database, username, role)
            + "COMMIT;",
            True,
        )
    system.audit(f"PostgreSQL user created: {username} on {database} ({role})")


def rotate_password(system, database, username):
    old = credentials(system, database, username)
    path = credentials_path(system, database, username)
    password = secrets.token_urlsafe(32)
    with system.transaction([path]):
        system.write(path, json.dumps(dict(old, password=password)))
        sql(system, f"ALTER ROLE \"{name(username)}\" PASSWORD '{password}';", True)
    system.audit("PostgreSQL password rotated: " + username)


def revoke(system, database, username):
    require_database(system, database)
    username = name(username)
    data = system.data()
    account = data.get("postgresql_accounts", {}).get(username)
    if not account or database not in account["grants"]:
        raise Error("db_unmanaged")
    del account["grants"][database]
    with system.transaction([STATE / "state.json"]):
        system.save(data)
        sql(
            system,
            "BEGIN;" + permission_sql(database, username, None) + "COMMIT;",
            True,
        )
    system.audit(f"PostgreSQL access revoked: {database} {username}")


def dump(system, database):
    database = require_database(system, database)
    target = (
        STATE / "database-dumps" / ("postgresql-" + database + "-" + stamp() + ".sql")
    )
    system.run_to_file(
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "pg_dump",
            "--create",
            "--dbname",
            database,
        ],
        target,
    )
    return str(target)


def remove(system, database, force=False):
    database = require_database(system, database)
    backup = dump(system, database)
    # FORCE is available only after the administrator explicitly confirms it.
    suffix = " WITH (FORCE)" if force else ""
    sql(system, f'DROP DATABASE "{database}"{suffix};', True)
    roles = groups(database)
    data = system.data()
    data["postgresql_databases"].pop(database)
    for info in data.get("postgresql_accounts", {}).values():
        info["grants"].pop(database, None)
    system.save(data)
    try:
        sql(
            system,
            "DROP ROLE " + ",".join('"' + v + '"' for v in roles.values()) + ";",
            True,
        )
    except Error:
        system.note("db_role_cleanup_warning")
        system.audit(
            "PostgreSQL database removed; retained dependent roles: " + database
        )
    system.audit("PostgreSQL database removed after SQL backup: " + database)
    return backup


def delete_user(system, database, username):
    path = credentials_path(system, database, username)
    sql(system, f'DROP ROLE "{name(username)}";', True)
    data = system.data()
    del data["postgresql_accounts"][username]
    system.save(data)
    system.delete(path)
    system.audit("PostgreSQL user deleted: " + username)


def inspect(system, database):
    database = name(database)
    if database not in {r["database"] for r in inventory(system)}:
        raise Error("db_unmanaged")
    result = sql(
        system,
        "SELECT schemaname||'.'||relname,n_live_tup,pg_total_relation_size(relid) "
        "FROM pg_stat_user_tables ORDER BY schemaname,relname;",
        database=database,
    )
    permissions = sql(
        system,
        "SELECT grantee,privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
        "ORDER BY grantee,privilege_type;",
        database=database,
    )
    return result, permissions


def console(system, database, statement):
    if database not in {r["database"] for r in inventory(system)}:
        raise Error("db_unmanaged")
    from .databases import query_result

    return query_result(
        system,
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "-X",
            "--set",
            "ON_ERROR_STOP=1",
            "-P",
            "pager=off",
            "--dbname",
            database,
        ],
        "SET statement_timeout=25000;\n" + statement,
    )
