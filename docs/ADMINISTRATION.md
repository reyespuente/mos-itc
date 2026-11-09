# Administrar el servidor con MOS ITC

## Actualizar desde 0.4

La instalación conserva `/etc/mos-itc`, el estado de MOS, los sitios, sus usuarios,
los certificados y los destinos de respaldo. No reinstala el servidor web ni
vuelve a ejecutar su configuración automática. Los campos nuevos usan valores
predeterminados y se guardan cuando modificas esa opción.

En **Software y actualizaciones → Actualizar MOS ITC** puedes comprobar los tags
publicados en el repositorio oficial y confirmar la actualización. Se descarga
el código del commit concreto de ese tag y se crea un punto de recuperación.
Al terminar, la aplicación vuelve a abrirse con la nueva versión. No se instala
ninguna actualización sin confirmación.

También puedes descargar el instalador del tag publicado y ejecutarlo con
`--upgrade`. Escribe `y`, después `ACCEPT` y finalmente `UPGRADE` cuando se
soliciten. Enter sin escribir la confirmación cancela. No borres ni cambies un
tag ya publicado para distribuir otro código: publica una versión nueva.

La actualización se bloquea si hay una operación de MOS activa o un cambio SSH
pendiente. El bloqueo se toma antes de capturar el punto de recuperación.

## Resumen y rendimiento

**Resumen** muestra barras de recursos, sistema operativo, direcciones con su
prefijo, puerta de enlace, zona horaria y número de sitios y certificados. Una
IP `/32` no se considera pública solo por su prefijo. En EC2 se consulta la IP
pública mediante IMDSv2; fuera de EC2 se usan las direcciones globales locales o
la IP que definas en **Configuración → Descargas**. No se envían datos a un
servicio externo de detección de IP.

**Ver rendimiento → Dashboard** actualiza las barras e historial de CPU,
memoria, disco, swap, inodes, tráfico, servicios y procesos. Las muestras de la
sesión son en vivo; el historial de siete días usa las muestras horarias del
perfil automático. Usa flechas para desplazarte y Esc para volver.

Las tablas se ajustan al ancho disponible. Los resultados SQL muy anchos conservan
sus columnas: usa **H/L** para desplazarte horizontalmente.

## Sitios y registros

Selecciona un dominio para ver sus datos, cambiar la versión PHP-FPM, el directorio
público, los alias, la zona horaria o el certificado. MOS comprueba la configuración
antes de recargar Apache y FPM. Elegir otra versión requiere instalarla primero en
**Software y actualizaciones → Paquetes**; solo se ofrecen versiones disponibles
en los repositorios autorizados del servidor.

**Registros del dominio** permite ver las últimas 200 líneas o seguir el archivo
en tiempo real. Después eliges accesos o errores de Apache. Ctrl+C vuelve desde
el seguimiento. Los registros internos de Laravel u otras aplicaciones dependen
de la configuración de cada proyecto.

## Bases de datos

MariaDB y PostgreSQL mantienen menús separados. Las bases locales creadas fuera
de MOS se pueden inspeccionar sin modificar sus propietarios ni sus permisos.
La inspección muestra tablas, tamaños legibles y permisos visibles para la cuenta
administrativa. No intenta descubrir contraseñas que MOS no haya creado.

En una base administrada puedes crear usuarios, compartir un usuario entre bases,
cambiar permisos, rotar contraseñas y exportar SQL. **Terminal SQL** también está
disponible al inspeccionar una base existente. Ejecuta como administrador local:
revisa la consulta antes de confirmarla. Usa `LIMIT`; hay un límite de tiempo de
30 segundos y la pantalla muestra como máximo 256 KiB de salida. La consulta y su
resultado no se guardan en el historial de operaciones de MOS.

Al eliminar PostgreSQL puedes confirmar el cierre de conexiones activas. Primero
se crea una copia SQL. Los archivos SQL guardados siguen disponibles en
**Respaldos → Archivos y descargas locales**, donde puedes descargarlos o borrarlos.

## Usuarios SSH

**Usuarios SSH** distingue administradores, cuentas locales y usuarios de sitios.
Un administrador recibe sudo general con contraseña. Un usuario normal recibe
Bash y una carpeta personal o un sitio como home, sin permisos sudo. Varios usuarios
pueden compartir el mismo sitio mediante ACL y permisos de archivos. Git, Composer
y npm se usan desde los paquetes instalados en el propio servidor.

No se utilizan contenedores ni una jaula de archivos. Al abrir SSH el usuario
comienza en su home; podrá recorrer otras rutas que permitan los permisos de Linux.
Los archivos privados de otros sitios mantienen sus permisos. Asignar una carpeta
no equivale a impedir el acceso a todo el resto del sistema.

Puedes administrar claves, contraseñas, asignación de carpeta y suspensión de
nuevos accesos. Cambiar de home no mueve ni borra datos. Suspender una cuenta no
cierra automáticamente sesiones que ya estuvieran abiertas. Los privilegios de
cuentas existentes se revisan por separado; no se eliminan silenciosamente.

La contraseña de la cuenta no cambia por sí sola la política SSH. Para permitir
acceso por contraseña o cambiar puertos, abre **Configuración → SSH**. Cada cambio
arma una recuperación persistente a los 180 segundos. Conserva tu sesión abierta,
autoriza el puerto en el firewall del proveedor y prueba una segunda conexión
antes de confirmar con el token. Si no confirmas, se recupera la configuración.

## Seguridad y certificados

**Seguridad → Administrar seguridad** incluye reglas UFW y los ajustes de Fail2ban.
El formulario de firewall permite origen, puerto, protocolo y acción. No permite
bloquear los puertos SSH actuales ni borrar reglas ambiguas que puedan contenerlos.
El firewall de EC2 o de otro proveedor se administra por separado.

Fail2ban tiene un formulario para jail, intentos, ventana, duración y activación,
y un editor avanzado de jails. Se comprueba la configuración y se espera a que el
servicio esté disponible antes de anunciar éxito.

El inventario SSL reúne certificados personalizados y los encontrados bajo
`/etc/letsencrypt/live`. Desde un certificado personalizado puedes reemplazarlo o
asignarlo. El botón principal **Agregar certificado personalizado** solo agrega;
no reemplaza por accidente un nombre existente.

Certbot comprueba diariamente qué certificados necesitan renovación. Puedes
cambiar la hora del servidor o ejecutar la comprobación manualmente; no se fuerza
la emisión de certificados que aún no necesitan renovarse. Conserva accesibles los
métodos de validación de tus dominios y revisa errores del timer.

## Configuración

El perfil automático se aplica una sola vez, incluyendo instalaciones 0.4 donde
ya se había aplicado. Después usa los formularios independientes. Cambiar nombre
o zona horaria no repite ese perfil; ten en cuenta la hora de tus tareas.

**PHP** separa versión y FPM/CLI. Los límites del formulario FPM se aplican a los
sitios administrados de esa versión y se conservan al editar un sitio. La zona
horaria del sitio y su aislamiento `open_basedir` se mantienen por separado.
En avanzado puedes editar `php.ini` o `99-mos-production.ini`; el archivo de MOS
y los valores de cada pool tienen prioridad sobre `php.ini`.

**Avanzada → Puntos de restauración** reúne puntos del instalador y copias creadas
al editar configuraciones. Puedes consultar su contenido, extraerlo a una carpeta
nueva y privada o borrarlo. No se sobrescribe el sistema en uso. Un punto no es
una imagen completa: no sustituye respaldos de sitios y bases de datos.

El editor APT admite `sources.list`, `.list` y `.sources`. Una validación fallida
recupera el archivo anterior. Las fuentes externas requieren iniciar MOS con la
opción explícita que muestra el error; no se agregan repositorios silenciosamente.
