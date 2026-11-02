#!/usr/bin/env bash
# Vigilante de batería para Sadida (host de Proxmox): la batería del portátil hace
# de UPS (DECISIONS.md D-012, sustituye a NUT en la Fase 1.6).
#
#   - En batería más de HALT_AFTER_SECONDS: activa el kill switch (/run/trading/HALT,
#     montado en el contenedor del Wheel, una de sus tres fuentes). Sin trading nuevo;
#     las posiciones y los stops del exchange siguen como estaban.
#   - Batería <= SHUTDOWN_BELOW_PCT: para los contenedores de trading con
#     `docker compose stop` (SIGTERM al Wheel, 120 s para que Postgres cierre limpio).
#   - Al volver la corriente NO levanta el kill switch: lo levanta una persona.
#
# Uso:  battery-watch.sh            bucle (lo lanza trading-battery-watch.service)
#       battery-watch.sh --once     una sola comprobación (tests)
#
# Configuración en /etc/trading/battery-watch.conf (COMPOSE_FILE obligatorio). Las
# variables POWER_SUPPLY_DIR, HALT_DIR, DOCKER, STATE_DIR y NOW existen para poder
# probarlo sin hardware.
set -euo pipefail

CONF=${CONF:-/etc/trading/battery-watch.conf}
# shellcheck source=/dev/null
[[ -r "$CONF" ]] && source "$CONF"

: "${COMPOSE_FILE:?define COMPOSE_FILE (docker/compose.runtime.yml del repo) en $CONF}"
HALT_AFTER_SECONDS=${HALT_AFTER_SECONDS:-120}   # ignora parpadeos de LUMA
SHUTDOWN_BELOW_PCT=${SHUTDOWN_BELOW_PCT:-25}
POLL_SECONDS=${POLL_SECONDS:-15}
POWER_SUPPLY_DIR=${POWER_SUPPLY_DIR:-/sys/class/power_supply}
HALT_DIR=${HALT_DIR:-/run/trading}
DOCKER=${DOCKER:-docker}
COMPOSE_ENV_FILE=${COMPOSE_ENV_FILE:-/etc/trading/compose.env}
STATE_DIR=${STATE_DIR:-/run/trading-battery-watch}

log() { logger -t trading-battery-watch -- "$*" 2>/dev/null || true; echo "$*"; }
now() { echo "${NOW:-$(date +%s)}"; }

on_mains() {
  # Corriente si algún adaptador de tipo Mains está online. Si no hay ninguno, se
  # mira el estado de la batería.
  local found=0 ps
  for ps in "$POWER_SUPPLY_DIR"/*; do
    [[ -r "$ps/type" ]] || continue
    if [[ $(<"$ps/type") == "Mains" ]]; then
      found=1
      [[ $(<"$ps/online") == "1" ]] && return 0
    fi
  done
  if (( found )); then return 1; fi
  for ps in "$POWER_SUPPLY_DIR"/*; do
    [[ -r "$ps/type" && $(<"$ps/type") == "Battery" ]] || continue
    [[ $(<"$ps/status") == "Discharging" ]] && return 1
  done
  return 0
}

battery_pct() {
  local ps
  for ps in "$POWER_SUPPLY_DIR"/*; do
    if [[ -r "$ps/type" && $(<"$ps/type") == "Battery" ]]; then
      cat "$ps/capacity"; return
    fi
  done
  echo 100
}

check() {
  mkdir -p "$STATE_DIR"
  local since_file="$STATE_DIR/on_battery_since" t pct since
  t=$(now)
  if on_mains; then
    if [[ -f "$since_file" ]]; then
      log "corriente restablecida tras $(( t - $(<"$since_file") )) s; el kill switch NO se levanta solo"
      rm -f "$since_file"
    fi
    return 0
  fi
  [[ -f "$since_file" ]] || { echo "$t" >"$since_file"; log "en batería"; }
  since=$(<"$since_file")
  pct=$(battery_pct)

  if (( t - since >= HALT_AFTER_SECONDS )) && [[ ! -f "$STATE_DIR/halted" ]]; then
    if mkdir -p "$HALT_DIR" && touch "$HALT_DIR/HALT"; then
      touch "$STATE_DIR/halted"
      log "en batería $(( t - since )) s (${pct}%): kill switch activado ($HALT_DIR/HALT)"
    else
      log "ERROR: no se pudo crear $HALT_DIR/HALT"
    fi
  fi
  if (( pct <= SHUTDOWN_BELOW_PCT )) && [[ ! -f "$STATE_DIR/shutdown" ]]; then
    log "batería al ${pct}% (<= ${SHUTDOWN_BELOW_PCT}%): parando los contenedores de trading"
    if "$DOCKER" compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV_FILE" stop; then
      touch "$STATE_DIR/shutdown"
    else
      log "ERROR: docker compose stop falló"
    fi
  fi
}

if [[ "${1:-}" == "--once" ]]; then
  check
  exit 0
fi
log "vigilando la batería (kill switch a los ${HALT_AFTER_SECONDS} s, parada al ${SHUTDOWN_BELOW_PCT}%)"
while true; do
  check || log "ERROR en la comprobación"
  sleep "$POLL_SECONDS"
done
