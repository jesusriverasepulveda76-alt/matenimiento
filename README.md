# Sistema de Mantencion OT (Nube + iPhone)

Aplicacion web en Flask para mantenimiento predictivo, preventivo y correctivo con OT, login por roles y PWA para iPhone.

## Funcionalidades

- Activos (motores, tableros, bombas, etc.)
- OT (crear, gestionar, cerrar)
- Mediciones (temperatura, amperaje, vibracion)
- Fallas e incidentes
- KPIs: MTBF, MTTR, cumplimiento preventivo
- Roles: `operator`, `planner`, `supervisor`
- PWA instalable en iPhone

## Credenciales iniciales

- Usuario: `admin`
- Clave: `admin12345`
- Rol: `supervisor`

## Ejecutar en local

```bash
python app.py
```

## Despliegue 24/7 en Render (recomendado)

Esta opcion deja la app funcionando aunque tu PC este apagado.

### 1) Subir proyecto a GitHub

Si no usas Git por consola, puedes crear un repo en GitHub y subir esta carpeta desde la interfaz web.

### 2) Crear servicio en Render

1. Entra a [Render](https://render.com) y conecta tu cuenta de GitHub.
2. Crea un nuevo `Blueprint` o `Web Service` desde este repo.
3. Render detectara `render.yaml` automaticamente.
4. Confirma el deploy.

### 3) Configuracion incluida

Este proyecto ya trae:

- `render.yaml` con:
  - `gunicorn` como servidor de produccion
  - `health check` en `/health`
  - disco persistente en `/var/data`
  - variable `MAINTENANCE_DB_PATH=/var/data/maintenance.db`
- `requirements.txt`
- `Procfile`

### 4) Cambiar clave admin en produccion

Al primer ingreso, entra a `Perfil` y cambia la clave.

Tambien puedes definir estas variables en Render:

- `MAINTENANCE_SECRET_KEY`
- `MAINTENANCE_DEFAULT_USER`
- `MAINTENANCE_DEFAULT_PASSWORD`
- `MAINTENANCE_DEFAULT_NAME`

## Instalar en iPhone

1. Abre la URL de Render en Safari.
2. Toca `Compartir`.
3. Toca `Agregar a pantalla de inicio`.

## Archivos de despliegue

- `render.yaml`
- `requirements.txt`
- `Procfile`
- `.gitignore`

## Notas de base de datos

- En nube: usa `/var/data/maintenance.db` (persistente)
- En local: usa carpeta temporal del sistema si no defines `MAINTENANCE_DB_PATH`
