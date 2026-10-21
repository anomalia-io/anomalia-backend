from fastapi import APIRouter, Depends, HTTPException
from typing import List
from ..database import get_db_connection
from ..models import AuthNodePayload
from ..auth import verify_admin_token, get_current_user_profiles

router = APIRouter(prefix="/api/infrastructure", tags=["Infrastructure"], dependencies=[Depends(verify_admin_token)])

def verify_infra_manager(profiles: List[str] = Depends(get_current_user_profiles)):
    """Verifica que el usuario posea el perfil de gestión de infraestructura."""
    if "Infra Management Profile" not in profiles and "admin" not in profiles:
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: Se requiere el perfil 'Infra Management Profile'."
        )
    return True

@router.get("/", dependencies=[Depends(verify_infra_manager)])
def get_infra_nodes():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, hostname, ip_address, service_ip, component_type, port, status, description FROM infrastructure_nodes ORDER BY id ASC;")
        rows = cursor.fetchall()
        nodes = []
        for r in rows:
            nodes.append({
                "id": r[0],
                "hostname": r[1],
                "ip_address": r[2],  # IP de Host
                "service_ip": r[3] if r[3] else r[2],  # IP de Servicio
                "component_type": r[4],
                "port": r[5],
                "status": r[6],
                "description": r[7],
                "actions": {
                    "can_update": True,
                    "update_endpoint": f"/api/infrastructure/{r[0]}/update"
                }
            })
        return {"nodes": nodes}
    finally:
        cursor.close()
        conn.close()

@router.put("/{node_id}", dependencies=[Depends(verify_infra_manager)])
def update_infra_node(node_id: int, payload: AuthNodePayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM infrastructure_nodes WHERE id = %s;", (node_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Nodo no encontrado.")

        svc_ip = payload.service_ip if payload.service_ip else payload.ip_address
        cursor.execute(
            """UPDATE infrastructure_nodes 
               SET hostname = %s, ip_address = %s, service_ip = %s, component_type = %s, port = %s, status = %s, description = %s, updated_at = CURRENT_TIMESTAMP 
               WHERE id = %s""",
            (payload.hostname, payload.ip_address, svc_ip, payload.component_type, payload.port, payload.status, payload.description, node_id)
        )
        conn.commit()
        return {"status": "success", "message": "Nodo actualizado correctamente"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@router.delete("/{node_id}", dependencies=[Depends(verify_infra_manager)])
def delete_infra_node(node_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM infrastructure_nodes WHERE id = %s;", (node_id,))
        conn.commit()
        return {"status": "success", "message": "Nodo eliminado correctamente"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()
