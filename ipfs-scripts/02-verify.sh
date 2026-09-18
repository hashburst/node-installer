#!/usr/bin/env bash
set -u

HB_ROOT="${HB_STORAGE_ROOT:-/var/lib/hashburst}"
PUBLIC_MODE="${HB_PUBLIC_IPFS_MODE:-auto}"

PUB_REPO="$HB_ROOT/ipfs-public"
PRV_REPO="$HB_ROOT/ipfs-private"

echo "=== servizi systemd ==="

if [ "$PUBLIC_MODE" = "disabled" ]; then
    echo "  ipfs-public: disabilitato per configurazione"
else
    systemctl is-active --quiet ipfs-public.service \
        && echo "  ipfs-public: attivo" \
        || echo "  ipfs-public: NON attivo"
fi

systemctl is-active --quiet ipfs-private.service \
    && echo "  ipfs-private: attivo" \
    || echo "  ipfs-private: NON attivo"

echo
echo "=== porte in ascolto ==="

ss -tlnp 2>/dev/null \
    | grep -E ':(4001|4011|5001|5011|8080|8090)\b' \
    || echo "  nessuna porta IPFS rilevata"

echo
echo "=== identita dei nodi ==="

if [ "$PUBLIC_MODE" = "disabled" ]; then
    echo "  PUBBLICO peerID: NON APPLICABILE"
else
    PUBLIC_ID="$(
        IPFS_PATH="$PUB_REPO" ipfs id -f='<id>' 2>/dev/null
    )" || PUBLIC_ID="FALLITO"

    echo "  PUBBLICO peerID: $PUBLIC_ID"
fi

PRIVATE_ID="$(
    IPFS_PATH="$PRV_REPO" ipfs id -f='<id>' 2>/dev/null
)" || PRIVATE_ID="FALLITO"

echo "  PRIVATO peerID: $PRIVATE_ID"

echo
echo "=== verifica rete privata ==="

if journalctl \
    -u ipfs-private.service \
    --no-pager \
    -n 100 2>/dev/null \
    | grep -qi 'limited to private network'
then
    echo "  CONFERMATO: swarm privato attivo"
else
    echo "  FALLITO: conferma rete privata non trovata"
fi

echo
echo "=== test funzionale add e cat ==="

TESTFILE="$(mktemp)"
trap 'rm -f "$TESTFILE"' EXIT

printf 'hashburst-ipfs-test-%s\n' "$(date +%s)" > "$TESTFILE"

if [ "$PUBLIC_MODE" = "disabled" ]; then
    echo "  pubblico add/cat: NON APPLICABILE"
else
    CID_PUBLIC="$(
        IPFS_PATH="$PUB_REPO" ipfs add -q "$TESTFILE" 2>/dev/null
    )" || CID_PUBLIC=""

    if [ -n "$CID_PUBLIC" ] && \
       IPFS_PATH="$PUB_REPO" ipfs cat "$CID_PUBLIC" >/dev/null 2>&1
    then
        echo "  pubblico add/cat: OK CID=$CID_PUBLIC"
    else
        echo "  pubblico add/cat: FALLITO"
    fi
fi

CID_PRIVATE="$(
    IPFS_PATH="$PRV_REPO" ipfs add -q "$TESTFILE" 2>/dev/null
)" || CID_PRIVATE=""

if [ -n "$CID_PRIVATE" ] && \
   IPFS_PATH="$PRV_REPO" ipfs cat "$CID_PRIVATE" >/dev/null 2>&1
then
    echo "  privato add/cat: OK CID=$CID_PRIVATE"
else
    echo "  privato add/cat: FALLITO"
fi

echo
echo "=== swarm key fingerprint ==="

if [ -s "$PRV_REPO/swarm.key" ]; then
    sha256sum "$PRV_REPO/swarm.key" \
        | awk '{print "  privato swarm.key sha256:", $1}'
else
    echo "  FALLITO: swarm.key assente"
fi
