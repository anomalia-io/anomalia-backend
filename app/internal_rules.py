"""Reglas canónicas de alerta para los tenants de control interno (spec 011 §4.7).

Las reglas viven en archivos YAML estáticos (internal_rules/<slug>.yaml) que se
copian al contenedor verbatim (rol 'backend': copy del arbol -> /opt/anomalia,
Dockerfile COPY . .). Al no pasar por Jinja, las anotaciones con {{ $labels.* }}
llegan intactas a vmalert.

La BD es la fuente de verdad de las reglas, pero para un tenant interno el PUT
esta bloqueado a nivel de API; el unico origen admisible es este modulo, por lo
que las reglas son inmutables por construccion (cualquier cambio es un commit).
"""
import logging
from pathlib import Path

import yaml

logger = logging.getLogger("anomalia.internal_rules")

_RULES_DIR = Path(__file__).parent / "internal_rules"

INTERNAL_RULES: dict[str, str] = {}
for _path in sorted(_RULES_DIR.glob("*.yaml")):
    _slug = _path.stem
    _content = _path.read_text(encoding="utf-8")
    try:
        _parsed = yaml.safe_load(_content)
    except yaml.YAMLError as exc:  # pragma: no cover - fail fast en arranque
        raise ValueError(f"Reglas internas inválidas para el tenant '{_slug}': {exc}") from exc

    # Un archivo puede ser YAML válido y, aun así, rechazado por el parser de
    # reglas de vmalert (spec 011 §4.9): todo ítem de 'groups' debe ser un
    # mapping con 'name' no vacío y 'rules' no vacío. Si no, vmalert no arranca
    # el unit del tenant interno, por lo que se falla rápido en el import.
    _groups = (_parsed or {}).get("groups") if isinstance(_parsed, dict) else None
    if not isinstance(_groups, list) or not _groups:
        raise ValueError(
            f"Reglas internas inválidas para el tenant '{_slug}': 'groups' debe "
            f"ser una lista no vacía (vmalert exige al menos un grupo)."
        )
    for _idx, _group in enumerate(_groups):
        if not isinstance(_group, dict):
            raise ValueError(
                f"Reglas internas inválidas para el tenant '{_slug}': el grupo "
                f"{_idx} no es un mapping (revisa la indentación en "
                f"{_path.name}; una alerta suelta a nivel de 'groups' rompe vmalert)."
            )
        if not _group.get("name"):
            raise ValueError(
                f"Reglas internas inválidas para el tenant '{_slug}': el grupo "
                f"{_idx} no tiene 'name'."
            )
        if not _group.get("rules"):
            raise ValueError(
                f"Reglas internas inválidas para el tenant '{_slug}': el grupo "
                f"'{_group.get('name')}' no tiene 'rules' o está vacío."
            )
    INTERNAL_RULES[_slug] = _content

logger.info("Reglas internas cargadas: %s", ", ".join(sorted(INTERNAL_RULES)))
