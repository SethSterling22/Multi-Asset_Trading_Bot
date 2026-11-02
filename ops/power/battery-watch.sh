#!/usr/bin/env bash
# Vigilante de batería para Sadida (host de Proxmox): la batería del portátil hace
# de UPS (DECISIONS.md D-011, sustituye a NUT en la Fase 1.6).
#
#   - En batería más de HALT_AFTER_SECONDS: activa el kill switch DENTRO de la VM
#     (/run/trading/HALT, una de sus tres fuentes) vía el agente de QEMU. Sin trading
#     nuevo; las posiciones y los stops del exchange siguen como estaban.
#   - Batería <= SHUTDOWN_BELOW_PCT: apaga la VM limpio (Postgres cierra bien).
#   - Al volver la corriente NO levanta el kill switch: lo levanta una persona.
#
# Uso:  battery-watch.sh            bucle (lo lanza trading-battery-watch.service)
#       battery-watch.sh --once     una sola comprobación (tests)
#
# Configuración en /etc/trading/battery-watch.conf (VMID obligatorio). Las variables
# POWER_SUPPLY_DIR, QM, STATE_DIR y NOW existen para poder probarlo sin hardware.
set -euo pipefail

CONF=${CONF:-/etc/trading/battery-watch.conf}
# shellcheck source=/dev/null
[[ -r "$CONF" ]] && source "$CONF"

: "${VMID:?define VMID (la VM de trading) en $CONF}"
HALT_AFTER_SECONDS=${HALT_AFTER_SECONDS:-120}   # ignora parpadeos de LUMA
SHUTDOWN_BELOW_PCT=${SHUTDOWN_BELOW_PCT:-25}
POLL_SECONDS=${POLL_SECONDS:-15}
POWER_SUPPLY_DIR=${POWER_SUPPLY_DIR:-/sys/class/power_supply}
QM=${QM:-qm}
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
    if "$QM" guest exec "$VMID" -- sh -c 'mkdir -p /run/trading && touch /run/trading/HALT'; then
      touch "$STATE_DIR/halted"
      log "en batería $(( t - since )) s (${pct}%): kill switch activado en la VM $VMID"
    else
      log "ERROR: no se pudo activar el kill switch en la VM $VMID (¿qemu-guest-agent?)"
    fi
  fi
  if (( pct <= SHUTDOWN_BELOW_PCT )) && [[ ! -f "$STATE_DIR/shutdown" ]]; then
    log "batería al ${pct}% (<= ${SHUTDOWN_BELOW_PCT}%): apagando la VM $VMID"
    if "$QM" shutdown "$VMID" --timeout 180; then
      touch "$STATE_DIR/shutdown"
    else
      log "ERROR: qm shutdown $VMID falló"
    fi
  fi
}

if [[ "${1:-}" == "--once" ]]; then
  check
  exit 0
fi
log "vigilando la batería (VM $VMID, kill switch a los ${HALT_AFTER_SECONDS} s, apagado al ${SHUTDOWN_BELOW_PCT}%)"
while true; do
  check || log "ERROR en la comprobación"
  sleep "$POLL_SECONDS"
done
