"""MariaDB administration, restricted to databases and accounts managed by MOS."""

import json
import secrets

from .core import CONF, STATE, SITES, Error, domain, name, stamp

SYSTEM_DATABASES = {"mysql", "sys", "information_schema", "performance_schema"}
ROLES = {
    "read": "SELECT, SHOW VIEW",
    "write": "SELECT, INSERT, UPDATE, DELETE, SHOW VIEW",
    "admin": "ALL PRIVILEGES",
}


def sql(system, statement, mutate=False):
    try:
        return system.run(
            ["mariadb", "--protocol=socket", "--batch", "--skip-column-names"],
            input=statement,
            mutate=mutate,
        )
    except Error:
        # MariaDB errors can echo a failed statement containing a password.
        raise Error("db_command_failed") from None


def managed(system):
    data = system.data()
    result = {name(db): dict(info) for db, info in data.get("databases", {}).items()}
    for host, site in data["sites"].items():
        if site.get("database"):
            result[name(site["database"])] = {
                "site": host,
                "primary": name(site["user"]),
            }
    return result


def require_database(system, database):
    database = name(database)
    if database in SYSTEM_DATABASES or database not in managed(system):
        raise Error("db_unmanaged")
    return database


def inventory(system):
    rows = sql(
        system,
        "SELECT s.SCHEMA_NAME, COUNT(t.TABLE_NAME), "
        "COALESCE(SUM(t.DATA_LENGTH+t.INDEX_LENGTH),0) "
        "FROM information_schema.SCHEMATA s LEFT JOIN information_schema.TABLES t "
        "ON t.TABLE_SCHEMA=s.SCHEMA_NAME GROUP BY s.SCHEMA_NAME ORDER BY s.SCHEMA_NAME;",
    )
    ownership = managed(system)
    return [
        {
            "database": db,
            "tables": int(count),
            "bytes": int(size),
            "managed": db in ownership,
            "site": ownership.get(db, {}).get("site", ""),
        }
        for line in rows.splitlines()
        if len(parts := line.split("\t")) == 3
        for db, count, size in [parts]
        if db not in SYSTEM_DATABASES
    ]


def create(system, database):
    database = name(database)
    if database in SYSTEM_DATABASES or database in {
        r["database"] for r in inventory(system)
    }:
        raise Error("db_exists")
    sql(
        system,
        f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        True,
    )
    data = system.data()
    data.setdefault("databases", {})[database] = {}
    try:
        system.save(data)
    except BaseException:
        sql(system, f"DROP DATABASE `{database}`;", True)
        raise
    system.audit("Database created: " + database)


def tables(system, database):
    database = require_database(system, database)
    return sql(
        system,
        "SELECT TABLE_NAME, ENGINE, COALESCE(TABLE_ROWS,0), "
        "COALESCE(DATA_LENGTH+INDEX_LENGTH,0) FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA='{database}' ORDER BY TABLE_NAME;",
    )


def accounts(system, database):
    database = require_database(system, database)
    result = []
    owner = managed(system)[database]
    if owner.get("primary"):
        result.append({"username": owner["primary"], "role": "admin", "primary": True})
    for user, info in system.data().get("database_accounts", {}).items():
        if database in info.get("grants", {}):
            result.append(
                {
                    "username": name(user),
                    "role": info["grants"][database],
                    "primary": False,
                }
            )
    return result


def credentials_path(system, database, username):
    if not any(a["username"] == username for a in accounts(system, database)):
        raise Error("db_unmanaged")
    owner = managed(system)[database]
    if username == owner.get("primary"):
        return SITES / domain(owner["site"]) / ".database.json"
    return CONF / "database-users" / "mariadb" / (name(username) + ".json")


def credentials(system, database, username):
    path = credentials_path(system, database, username)
    try:
        value = json.loads(system.read(path))
        if value["username"] != username or value["host"] != "localhost":
            raise ValueError
        return {
            "database": database,
            "username": username,
            "host": "localhost",
            "password": value["password"],
        }
    except (ValueError, KeyError, TypeError):
        raise Error("db_credentials_missing") from None


def privilege_target(database):
    # MariaDB treats underscores as wildcards even in quoted GRANT identifiers.
    return "`" + name(database).replace("_", "\\_") + "`.*"


def grant(system, database, username, role):
    database = require_database(system, database)
    username = name(username)
    if role not in ROLES:
        raise Error("db_invalid_role")
    data = system.data()
    account = data.get("database_accounts", {}).get(username)
    if account is None:
        raise Error("db_unmanaged")
    old = account["grants"].get(database)
    account["grants"][database] = role
    with system.transaction([STATE / "state.json"]):
        system.save(data)
        if old:
            sql(
                system,
                f"REVOKE ALL PRIVILEGES ON {privilege_target(database)} FROM '{username}'@'localhost';",
                True,
            )
        try:
            sql(
                system,
                f"GRANT {ROLES[role]} ON {privilege_target(database)} TO '{username}'@'localhost';",
                True,
            )
        except Error:
            if old:
                sql(
                    system,
                    f"GRANT {ROLES[old]} ON {privilege_target(database)} TO '{username}'@'localhost';",
                    True,
                )
            raise
    system.audit(f"Database permission changed: {database} {username} {role}")


def create_user(system, database, username, role):
    database = require_database(system, database)
    username = name(username)
    if role not in ROLES:
        raise Error("db_invalid_role")
    if username in (
        "root",
        "mysql",
        "mariadb",
        "mariadb.sys",
    ) or username in system.data().get("database_accounts", {}):
        raise Error("db_user_exists")
    if sql(system, f"SELECT User FROM mysql.user WHERE User='{username}';").strip():
        raise Error("db_user_exists")
    password = secrets.token_urlsafe(32)
    path = CONF / "database-users" / "mariadb" / (username + ".json")
    if system.exists(path) or path.is_symlink():
        raise Error("db_user_exists")
    sql(
        system,
        f"CREATE USER '{username}'@'localhost' IDENTIFIED BY '{password}';",
        True,
    )
    try:
        sql(
            system,
            f"GRANT {ROLES[role]} ON {privilege_target(database)} TO '{username}'@'localhost';",
            True,
        )
        system.write(
            path,
            json.dumps(
                {"username": username, "host": "localhost", "password": password}
            ),
        )
        data = system.data()
        data.setdefault("database_accounts", {})[username] = {
            "grants": {database: role}
        }
        system.save(data)
    except BaseException:
        sql(system, f"DROP USER IF EXISTS '{username}'@'localhost';", True)
        system.delete(path)
        raise
    system.audit(f"Database user created: {username} on {database} ({role})")


def rotate_password(system, database, username):
    old = credentials(system, database, username)
    path = credentials_path(system, database, username)
    password = secrets.token_urlsafe(32)
    owner = managed(system)[database]
    try:
        with system.transaction([path]):
            system.write(path, json.dumps(dict(old, password=password), indent=2))
            sql(
                system,
                f"ALTER USER '{name(username)}'@'localhost' IDENTIFIED BY '{password}';",
                True,
            )
    finally:
        if owner.get("primary") == username:
            system.run(["chown", f"{username}:{username}", path], mutate=True)
    system.audit("Database password rotated: " + username)


def revoke(system, database, username):
    database = require_database(system, database)
    username = name(username)
    data = system.data()
    account = data.get("database_accounts", {}).get(username)
    if account is None or database not in account["grants"]:
        raise Error("db_primary_protected")
    del account["grants"][database]
    with system.transaction([STATE / "state.json"]):
        system.save(data)
        sql(
            system,
            f"REVOKE ALL PRIVILEGES ON {privilege_target(database)} FROM '{username}'@'localhost';",
            True,
        )
    system.audit(f"Database access revoked: {database} {username}")


def dump(system, database):
    database = require_database(system, database)
    target = STATE / "database-dumps" / (database + "-" + stamp() + ".sql")
    system.run_to_file(
        [
            "mariadb-dump",
            "--protocol=socket",
            "--single-transaction",
            "--routines",
            "--events",
            "--triggers",
            "--databases",
            database,
        ],
        target,
    )
    return str(target)


def remove(system, database):
    database = require_database(system, database)
    backup = dump(system, database)
    for account in accounts(system, database):
        if not account["primary"]:
            revoke(system, database, account["username"])
    owner = managed(system)[database]
    sql(system, f"DROP DATABASE IF EXISTS `{database}`;", True)
    if owner.get("primary"):
        sql(
            system, f"DROP USER IF EXISTS '{name(owner['primary'])}'@'localhost';", True
        )
        system.delete(SITES / domain(owner["site"]) / ".database.json")
    data = system.data()
    data.get("databases", {}).pop(database, None)
    for site in data["sites"].values():
        if site.get("database") == database:
            site.pop("database", None)
    system.save(data)
    system.audit("Database removed after SQL backup: " + database)
    return backup


def delete_user(system, database, username):
    path = credentials_path(system, database, username)
    data = system.data()
    if username not in data.get("database_accounts", {}):
        raise Error("db_primary_protected")
    sql(system, f"DROP USER '{name(username)}'@'localhost';", True)
    del data["database_accounts"][username]
    system.save(data)
    system.delete(path)
    system.audit("Database user deleted: " + username)
