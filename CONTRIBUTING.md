# Contribuir

## Flujo

1. Creá una rama desde `main` (`git checkout -b feat/tarea`).
2. Escribí código + tests.
3. Corré los checks locales (ver `README.md`).
4. ABRÍ un **Pull Request** con el template de `.github/`.
5. CI debe estar verde; un mantenedor revisa y mergea.

## Reglas

- **Commits firmados** (GPG/SSH).
- **No commits a `main` directo**.
- **Specs primero (SSD):** si el cambio afecta comportamiento documentado, actualizá la spec en `anomalia-meta`; si es funcionalidad nueva, creá la spec antes.
- **Sin secretos:** `gitleaks dir .` debe estar limpio; los secretos se referencean por variable de entorno.
- Mensajes de commit claros y concisos.

## Checks locales

```bash
# según el repo (ver README.md)
pytest -q
flutter analyze && flutter test --dart-define=FLUTTER_TEST=true
ansible-playbook -i inventory.ini deploy-infra.yml --syntax-check
gitleaks dir .
```
