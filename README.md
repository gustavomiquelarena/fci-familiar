# Fondo familiar

Administrador local de inversiones con Python, SQLite y reportes individuales cifrados en GitHub Pages.

La base principal y las credenciales quedan en la PC. El repositorio contiene el programa y los archivos cifrados destinados a la web. El administrador sólo escucha en `127.0.0.1`.

## Uso diario

1. Abrir `scripts/start-admin.ps1` con PowerShell (o el acceso `Abrir administrador familiar.cmd` junto a la carpeta del proyecto). Se abre el navegador y el servidor queda en segundo plano.
2. Ir a `http://127.0.0.1:8765`.
3. En **Movimientos**, registrar una suscripción o rescate y distribuir el importe entre los beneficiarios. Se puede ingresar USD o ARS.
4. Se usa el último valor de cuotaparte guardado correspondiente a la fecha. Los ARS se convierten con el MEP de ese mismo cierre. El precio, tipo de cambio y cuotapartes quedan congelados en el registro.
5. Si el dinero aún no está acreditado, dejarlo pendiente; marcarlo acreditado cuando corresponda.
6. Los movimientos nuevos se publican con el siguiente cierre. Registrar un movimiento no cambia el último reporte ya publicado.

Un rescate no puede superar las cuotapartes disponibles. Para corregir un movimiento se registra una reversión con motivo y luego el movimiento correcto. Los registros originales permanecen auditables.

Para cerrar el servidor, ejecutar `scripts/stop-admin.ps1` o el acceso `Cerrar administrador familiar.cmd` una vez terminadas las operaciones. Si ya está abierto, el acceso de inicio reutiliza la misma aplicación.

## Contraseñas de los hijos

En **Usuarios**, elegir la contraseña de cada hijo y confirmar. Se permiten contraseñas simples, sin requisitos de mayúsculas ni caracteres especiales. Se guardan en Windows Credential Manager bajo destinos exclusivos de esta aplicación.

El reseteo no pide la contraseña anterior. Republica el último cierre publicado con la nueva contraseña, sin adelantar movimientos ni cierres locales más nuevos. Si Git o Pages todavía no confirmaron el cambio, el administrador lo muestra pendiente; la contraseña anterior continúa correspondiendo a la versión anterior.

Los reportes usan AES-256-GCM, PBKDF2-SHA256 (600.000 iteraciones), sal de 16 bytes y nonce de 12 bytes nuevos por archivo. El navegador descifra con Web Crypto y mantiene la información sólo en memoria. No hay correo, servidor remoto ni recuperación automática de contraseña. El reseteo no revoca copias anteriores ya descargadas.

## Instalación en otra PC Windows

Requiere Python 3.11 o posterior, Git y acceso IOL habilitado para la API. La identidad Windows debe tener acceso a la clave Fernet existente.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
$env:PYTHONPATH = (Resolve-Path .\local).Path
.\.venv\Scripts\python.exe -m fci_familiar init --source 'C:\ruta\historico.xlsx' --encrypted-config 'C:\ruta\encrypted_config.ini' --iol-user 'usuarioIOL' --backup-dir 'C:\ruta\respaldos_privados'
powershell -ExecutionPolicy Bypass -File .\scripts\start-admin.ps1
```

La inicialización conserva los valores históricos de cuotapartes y los cierres guardados del Excel, incluyendo columnas auxiliares. Extrae las consultas Power Query para procedencia. No necesita que se actualicen fórmulas ni abrir Excel y no modifica el archivo original.

La configuración se guarda en `%LOCALAPPDATA%\FCIFamiliar\config.json`. Un puntero privado `.fci-familiar-local.json`, junto a la carpeta del checkout y fuera de Git, conserva su ubicación física para que Explorer, terminales empaquetadas y Task Scheduler abran la misma base. Para usar otra carpeta privada, agregar `--data-dir 'C:\ruta\privada'` antes del subcomando o definir `FCI_DATA_DIR`. La carpeta privada no puede estar dentro del checkout público.

El archivo heredado `encrypted_config.ini` contiene un objeto JSON cifrado con Fernet, no un INI convencional. La clave maestra se lee del destino genérico `secret_key` de Windows; la entrada `IOL` contiene la credencial del proveedor. No se guarda la clave maestra en el proyecto. El backend usa pywin32 si está disponible y la API nativa de Windows en caso contrario.

## Actualización y conciliación

**Actualizar IOL** consulta exclusivamente recursos de lectura del proveedor, además de la autenticación. Descarga órdenes, cuenta, cartera y cotizaciones. Las respuestas originales quedan en la carpeta privada `raw`.

- Las órdenes se identifican por cuenta y número. Se conservan registros informativos del proveedor sin importe operado, pero no se convierten en movimientos familiares ni se suman como transacciones monetarias.
- Los cambios del proveedor se auditan; los registros ausentes en una nueva descarga no se borran.
- Se revisan los últimos 30 días y, una vez al mes, el período completo desde el inicio del fondo.
- La cantidad de cuotapartes de cada cierre conserva la lista exacta de movimientos incluidos. Altas o correcciones posteriores no alteran ese cierre.
- El total del fondo se calcula con el efectivo y títulos de la cuenta, verificando que la cartera no duplique monedas y que concilie con los títulos valorizados.
- Los fondos deben asignarse manualmente a sus beneficiarios; IOL no identifica a qué hijo pertenece cada ingreso.

### Cierre MEP

Se toman series históricas de AL30 y AL30D, con precios del mismo día y MEP redondeado a dos decimales para mantener la definición del Excel. Una fecha se incorpora desde el día siguiente, según Argentina. No se consulta la cotización en vivo para el histórico.

El viernes se conserva al descargar el domingo. Las fechas recientes se vuelven a consultar y se actualizan por instrumento y fecha, sin duplicarlas ni eliminar el último cierre válido por su posición. El reporte muestra la fecha de corte de la cuenta y la cotización utilizada.

### Resultados

Los reportes se presentan únicamente en USD MEP. La ganancia monetaria es saldo más retiros menos aportes. Las suscripciones no se muestran como rendimiento. Los porcentajes de cambio del valor de cuotaparte describen el fondo, no una tasa personal anualizada.

La distribución por instrumento de cada hijo es proporcional a sus cuotapartes del fondo. El detalle histórico por instrumento existe desde las nuevas descargas de cartera; no se inventan composiciones para fechas donde el Excel sólo guardaba totales.

## GitHub Pages

En **Configuración**, indicar la URL HTTPS del repositorio y la URL de Pages. Para esta aplicación se publica la rama `main`, carpeta `/docs`, mediante **Settings → Pages → Deploy from a branch**.

Git debe tener configurado el nombre/correo del autor y un acceso válido al repositorio bajo el mismo usuario de Windows que ejecuta la tarea. Usar Git Credential Manager para iniciar sesión; nunca colocar un token en la URL del remoto ni en los archivos del proyecto.

**Publicar** prepara una versión completa, cifra cada reporte con su contraseña individual, comprueba los archivos permitidos y hace commit/push únicamente de los reportes generados. Los cambios del código se revisan y publican por separado. La versión sólo figura publicada cuando la URL de Pages confirma el manifiesto y sus archivos. Si Pages tarda más, la publicación queda pendiente de verificación.

La información financiera sin cifrar, bases, fuentes Excel, archivos de credenciales, respuestas IOL y respaldos se mantienen fuera de Git. Los datos de prueba son ficticios.

## Tarea semanal

Desde el administrador, **Programar actualización**, o ejecutar:

```powershell
$env:PYTHONPATH = (Resolve-Path .\local).Path
.\.venv\Scripts\python.exe -m fci_familiar schedule
```

Se registra `FCIFamiliar-Semanal`: domingos a las 18:00, bajo el usuario actual, con recuperación al iniciar sesión. La PC debe tener configurada la zona horaria de Argentina. Se usa inicio de sesión interactivo para conservar el acceso al almacén de credenciales; si no hay sesión, se recupera al siguiente inicio.

La tarea hace descarga, conciliación, publicación verificada y respaldo. No considera terminada una semana si sólo terminó la descarga. Un bloqueo del proceso evita dos actualizaciones simultáneas. Los errores quedan en `logs/weekly.log`, dentro de la carpeta privada.

Comandos adicionales:

```powershell
python -m fci_familiar sync
python -m fci_familiar publish
python -m fci_familiar prepare
python -m fci_familiar backup
python -m fci_familiar weekly --if-due
```

## Respaldos y recuperación

Los respaldos usan la API de SQLite y verifican integridad. Se conservan 12 semanales y 12 mensuales; la configuración se respalda separadamente. Las contraseñas y la clave maestra siguen en Windows Credential Manager y no se incluyen en SQLite.

Restaurar en una carpeta nueva (el comando no sobrescribe la base activa):

```powershell
python -m fci_familiar --data-dir 'C:\ruta\recuperacion' restore 'C:\ruta\respaldos_privados\weekly-AAAA-WNN.sqlite3'
python -m fci_familiar --data-dir 'C:\ruta\recuperacion' serve --port 8766
```

Si está disponible `config-backup.json` junto al respaldo, se recuperan sus ajustes manteniendo la nueva ubicación de datos. En otra cuenta Windows habrá que disponer de su clave Fernet y restablecer las contraseñas de los reportes. Para recuperar una versión web anterior, ir a **Actividad y respaldos → Republicar este cierre**. Se crea una publicación nueva del cierre seleccionado con las contraseñas actuales, sin modificar el libro de movimientos. Las versiones del código se recuperan por Git.

## Verificación del desarrollo

```powershell
$env:PYTHONPATH = (Resolve-Path .\local).Path
python -m unittest discover -s tests -v
node tests/webcrypto_test.mjs
```

Las pruebas usan datos sintéticos y dobles de la red/almacén. La prueba de migración del archivo original se omite si ese archivo privado no existe junto al checkout. Las pruebas cubren idempotencia, conciliaciones, límites de rescates, membresía de cierres, cotizaciones, errores HTTP, cifrado Python/navegador y estados de publicación.
