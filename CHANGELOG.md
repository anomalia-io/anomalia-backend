# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Silencios planificados de `VmalertTenantDown` durante maintenance de vmalert (deploy, undeploy, redeploy y actualización de nodos), con espera a que se despeje la alerta antes de levantar el silence cuando aplica.
- Asignación remota del puerto vmalert por tenant mediante `allocate-vmalert-port.yml`; el worker conserva el puerto devuelto por Ansible y solo corrige valores inválidos u ocupados.
- Detalle de alertas en `modal-alert-detail` y diálogos in-app con `modal-confirm` / `modal-prompt` en lugar de `confirm()` / `prompt()` del navegador.
- Logo web versionado (`logo.png`, con copia de respaldo `logo_bkp.png`).

### Changed
- El webhook de Alertmanager resuelve el tenant por precedencia `tenant_id` → `exported_tenant_slug` → `tenant_slug` → `org_id` / `org_id_upper` y cierra las filas `firing` huérfanas por labels en lugar de insertar una alerta sin tenant.
- El alta de tenant permanece `provisioning` hasta los sondeos de ingesta y vmalert (`waiting_ready`); el regen de scrapes/rutas lo encola el orquestador (`skip_regen`) y el rollback regenera rutas tras la limpieza.

### Fixed
- `recent` e `history` excluyen las filas `firing` con `tenant_id IS NULL` y labels de tenant; la baja de tenant cierra también sus alertas activas por tenant/labels.

## [0.1.0] - 2026-10-20

### Added
- Initial import from the `anomalia-project` monorepo.
