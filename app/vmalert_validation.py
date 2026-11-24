from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


class VmalertIssue(BaseModel):
    line: Optional[int] = None
    column: Optional[int] = None
    message: str
    severity: str = "error"


class VmalertValidation(BaseModel):
    valid: bool
    issues: List[VmalertIssue] = Field(default_factory=list)


def _issue(message: str, line: Optional[int] = None, column: Optional[int] = None) -> VmalertIssue:
    return VmalertIssue(line=line, column=column, message=message)


def validate_vmalert_yaml(text: Optional[str]) -> VmalertValidation:
    if text is None or not text.strip():
        return VmalertValidation(valid=True)

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return VmalertValidation(
            valid=False,
            issues=[
                _issue(
                    str(exc).strip(),
                    line=(mark.line + 1) if mark else None,
                    column=(mark.column + 1) if mark else None,
                )
            ],
        )

    issues: List[VmalertIssue] = []
    groups = parsed.get("groups") if isinstance(parsed, dict) else None
    if not isinstance(groups, list) or not groups:
        issues.append(_issue("'groups' debe ser una lista no vacía (vmalert exige al menos un grupo)."))
        return VmalertValidation(valid=False, issues=issues)

    for group_index, group in enumerate(groups):
        group_label = "grupo %s" % group_index
        if not isinstance(group, dict):
            issues.append(
                _issue(
                    "El %s no es un mapping; revisá la indentación: cada grupo debe empezar con '  - name:'." % group_label
                )
            )
            continue

        name = group.get("name")
        if not name:
            issues.append(_issue("El %s no tiene 'name'." % group_label))

        rules = group.get("rules")
        if not isinstance(rules, list) or not rules:
            issues.append(
                _issue("El grupo '%s' no tiene 'rules' o está vacío." % (name or group_label))
            )
            continue

        for rule_index, rule in enumerate(rules):
            rule_label = "regla %s del grupo '%s'" % (rule_index, name or group_label)
            if not isinstance(rule, dict):
                issues.append(_issue("La %s no es un mapping; revisá la indentación." % rule_label))
                continue
            if not rule.get("alert"):
                issues.append(_issue("La %s no tiene 'alert'." % rule_label))
            if not rule.get("expr"):
                issues.append(_issue("La %s no tiene 'expr'." % rule_label))

    return VmalertValidation(valid=not issues, issues=issues)


def format_validation_summary(validation: Optional[VmalertValidation]) -> str:
    if validation is None or validation.valid:
        return "\n- Validación del YAML del editor: sin errores de sintaxis detectados."

    lines = ["\n- Validación del YAML del editor: ERRORES DETECTADOS"]
    for issue in validation.issues:
        location = ""
        if issue.line is not None:
            location = "línea %s" % issue.line
            if issue.column is not None:
                location += ":%s" % issue.column
            location += " · "
        lines.append("  - %s%s" % (location, issue.message))
    lines.append(
        "  - Explicá cada error antes que cualquier otra recomendación y sugerí la corrección concreta."
    )
    return "\n".join(lines)
