#!/usr/bin/env bash
# Sincroniza data/ con el bucket de S3 (datos fuera de git).
#
#   scripts/datos_s3.sh bajar [--dryrun]   # S3 -> data/
#   scripts/datos_s3.sh subir [--dryrun]   # data/ -> S3
#
# Variables opcionales:
#   RELEVO_S3_BUCKET   bucket (default: relevo-motor-scoring)
#   RELEVO_S3_PREFIJO  prefijo (default: data)
#   RELEVO_AWS_PERFIL  perfil de AWS CLI (default: relevo-s3)
#
# relevo.sqlite no se sincroniza: es la base de trabajo de cada persona
# (solicitudes y evaluaciones) y no se comparte. Nunca borra nada: ni en el
# bucket ni en data/.

set -euo pipefail

BUCKET="${RELEVO_S3_BUCKET:-relevo-motor-scoring}"
PREFIJO="${RELEVO_S3_PREFIJO:-data}"
PERFIL="${RELEVO_AWS_PERFIL:-relevo-s3}"

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL="$RAIZ/data"
REMOTO="s3://$BUCKET/$PREFIJO/"

uso() {
    echo "Uso: $0 {bajar|subir} [--dryrun]" >&2
    exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || uso
accion="$1"
extra=()
if [[ $# -eq 2 ]]; then
    [[ "$2" == "--dryrun" ]] || uso
    extra=(--dryrun)
fi

command -v aws >/dev/null || { echo "Falta AWS CLI (brew install awscli)." >&2; exit 1; }
aws sts get-caller-identity --profile "$PERFIL" >/dev/null 2>&1 \
    || { echo "El perfil '$PERFIL' no existe o no tiene credenciales válidas (aws configure --profile $PERFIL)." >&2; exit 1; }

excluir=(--exclude "relevo.sqlite" --exclude "*.DS_Store")

case "$accion" in
    bajar)
        mkdir -p "$LOCAL"
        aws s3 sync "$REMOTO" "$LOCAL/" --profile "$PERFIL" "${excluir[@]}" ${extra[@]+"${extra[@]}"}
        ;;
    subir)
        [[ -d "$LOCAL" ]] || { echo "No existe $LOCAL." >&2; exit 1; }
        aws s3 sync "$LOCAL/" "$REMOTO" --profile "$PERFIL" "${excluir[@]}" ${extra[@]+"${extra[@]}"}
        ;;
    *)
        uso
        ;;
esac
