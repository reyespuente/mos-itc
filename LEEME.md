# MOS ITC — Inicio rápido

Herramienta de terminal creada por **Reyes Guadalupe Puente Balcazar** como una
iniciativa en el Instituto Tecnológico de Celaya. Licencia MIT, de libre uso.

Esta es la **versión 0.2.1-beta.1**. Prueba la instalación completa en una VM
desechable antes de utilizarla en un VPS con datos reales.

## Abrir los menús en Linux Mint sin instalar servicios

Desde la carpeta del proyecto:

```bash
python3 -m mos_itc --dry-run --language es
```

Flechas: moverse. Enter: seleccionar. Espacio: marcar paquetes.
Esc: volver. En listas largas puedes usar AvPág y RePág.

La vista previa no modifica el sistema. Las operaciones que requieren cambios se
imprimen y los archivos simulados viven solo en memoria durante esa ejecución.
No produce resultados de velocidad ficticios.

## Instalar dentro de tu VM

Empieza con **Debian 13** o **Ubuntu Server 24.04**, arquitectura x86_64, con systemd.
Copia la carpeta del proyecto a la VM y ejecuta desde ella:

```bash
sudo bash install.sh
sudo mos-itc --language es
```

Confirma que deseas continuar y escribe `ACCEPT` para aceptar la licencia MIT.
Después abre **Instalar servidor web → Instalación predeterminada**, revisa el
plan de paquetes y confirma su aplicación. La configuración automática pregunta
zona horaria, nombre del servidor y versión PHP predeterminada.

El menú **Hacer una prueba de velocidad** instala `speedtest-cli` si hace falta,
previa confirmación, y muestra descarga, subida, latencia, proveedor y servidor.
El encabezado ASCII usa el azul y verde publicados por el ITC.

## Traducciones y organización

* `languages/en.json`: inglés.
* `languages/es.json`: español latinoamericano.
* `mos_itc/i18n.py`: carga los catálogos y usa inglés como respaldo.

Ambos archivos comparten claves estables y parámetros con nombre, por ejemplo
`{domain}`. Las pruebas comprueban que coincidan las claves y los parámetros.
La lógica de administración está separada de los menús y de las traducciones.
Los nombres de programas y la salida original de herramientas del sistema se
conservan. El instalador inicial muestra textos bilingües antes de instalar Python.

## Aspectos que debes conocer

* Se usan repositorios oficiales por defecto. PHP 8.3, 8.4 y 8.5 solo se ofrecen
  donde APT tenga esos paquetes. Los sistemas anteriores pueden ofrecer su PHP
  mantenido por la distribución. No se agrega ningún repositorio externo solo.
* Los sitios gestionados usan Apache y usuarios Linux/PHP-FPM independientes.
  Esto no equivale a contenedores para ejecutar código de alumnos sin confianza.
* SFTP restringe los comandos, pero esta beta no crea un chroot.
* La importación de sitios existentes es beta y copia archivos: no migra bases,
  certificados ni reglas Apache personalizadas.
* Los respaldos con Restic deben inicializarse y probarse con un destino real.
  Conserva la contraseña fuera del servidor.
* Los cambios SSH tienen recuperación automática a los 180 segundos. Cuando
  trabajes remotamente, usa `sudo --preserve-env=SSH_CONNECTION mos-itc ssh manage`
  y conserva esa variable al confirmar desde una conexión nueva. El firewall del
  proveedor VPS se administra por separado.

La guía completa está en [README.md](README.md), las pruebas de aceptación de VM
en [TESTING.md](TESTING.md), y los límites de seguridad en [SECURITY.md](SECURITY.md).
Se validó la instalación y operación en Ubuntu 26.04.1; las demás versiones
de Debian/Ubuntu aún requieren sus propias pruebas de aceptación. Repositorio: https://github.com/reyespuente/mos-itc.

## Interfaz, bienvenida SSH y recuperación

Los menús permanecen abiertos y recuerdan la selección. Los formularios y las
confirmaciones usan la misma pantalla. En casillas: Espacio marca, Tab va a Aplicar.
No hay pausas adicionales de “presiona Enter para continuar”.

Al entrar por SSH aparecen solo el ASCII MOS ITC y `Control panel: sudo mos-itc`;
el comando va en verde ITC. El nombre del instituto y Reyes Puente están dentro
de la aplicación. La atribución completa y la licencia se encuentran en Acerca de.

Cada instalación/actualización crea un punto de configuración en
`/var/backups/mos-itc/restore-points`. Incluye `/etc`, inventario de paquetes y
servicios, y la aplicación anterior si existía. No es una imagen de disco ni un
respaldo de bases de datos. Extrae en un directorio nuevo y revisa antes de restaurar:

```bash
sudo mos-itc restore-point list
sudo mos-itc restore-point extract NOMBRE_DEL_PUNTO /srv/mos-recovery
sudo bash install.sh --upgrade
```

Las versiones de PHP se descubren desde APT, sin un límite de rama fijo. Las
actualizaciones se simulan y el plan se vuelve a comprobar antes de aplicar.
Consulta TEST_RESULTS.md para las pruebas reales de esta entrega; las futuras
versiones con cambios incompatibles deben validarse antes en una VM.

## Certificados y administradores SSH

El formulario de certificados separa certificado, clave privada y cadena CA.
En cada campo puedes pegar el PEM en nano o importar un archivo del servidor.
Un fullchain se separa automáticamente. Antes de guardar se revisan la vigencia,
la clave, la cadena y los dominios afectados. Los errores conservan el borrador;
si Apache no recarga, se recuperan los archivos y el inventario anteriores.

En **Administradores SSH** puedes crear usuarios con carpeta personal, Bash y
sudo completo que requiere su contraseña. También puedes cambiar contraseñas
y agregar claves públicas. Si SSH no permite contraseñas, se exige una clave.
La app no cambia esa política ni deshabilita root. Comando: `sudo mos-itc users`.
