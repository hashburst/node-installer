#!/bin/bash
# Verifica post-installazione dei due daemon IPFS sul nodo 0.
PUB_REPO="/datapool/hashburst/ipfs-public"
PRV_REPO="/datapool/hashburst/ipfs-private"

echo "=== servizi systemd ==="
systemctl is-active ipfs-public  && echo "  ipfs-public:  attivo"  || echo "  ipfs-public:  NON attivo"
systemctl is-active ipfs-private && echo "  ipfs-private: attivo"  || echo "  ipfs-private: NON attivo"

echo
echo "=== porte in ascolto (API su localhost, swarm aperti) ==="
ss -tlnp 2>/dev/null | grep -E ":5001|:5011|:8080|:8090|:4001|:4011" || echo "  (nessuna, i daemon potrebbero non essere pronti)"

echo
echo "=== identita' dei due nodi ==="
echo "  PUBBLICO peerID:  $(IPFS_PATH=$PUB_REPO ipfs id -f='<id>' 2>/dev/null || echo FALLITO)"
echo "  PRIVATO  peerID:  $(IPFS_PATH=$PRV_REPO ipfs id -f='<id>' 2>/dev/null || echo FALLITO)"

echo
echo "=== il privato e' DAVVERO in modalita' rete privata? ==="
# nei log del daemon privato deve comparire "Swarm is limited to private network"
journalctl -u ipfs-private --no-pager -n 30 2>/dev/null | grep -i "private network" \
  && echo "  CONFERMATO: swarm privato attivo" \
  || echo "  ATTENZIONE: messaggio 'private network' non trovato nei log (controlla swarm.key)"

echo
echo "=== test funzionale: add+cat su ciascun daemon ==="
TESTFILE=$(mktemp)
echo "hashburst-ipfs-test-$(date +%s)" > "$TESTFILE"

echo -n "  pubblico add: "
CID_PUB=$(IPFS_PATH=$PUB_REPO ipfs add -q "$TESTFILE" 2>/dev/null) && echo "$CID_PUB" || echo FALLITO
if [ -n "${CID_PUB:-}" ]; then
  IPFS_PATH=$PUB_REPO ipfs cat "$CID_PUB" >/dev/null 2>&1 && echo "  pubblico cat: OK" || echo "  pubblico cat: FALLITO"
fi

echo -n "  privato add: "
CID_PRV=$(IPFS_PATH=$PRV_REPO ipfs add -q "$TESTFILE" 2>/dev/null) && echo "$CID_PRV" || echo FALLITO
if [ -n "${CID_PRV:-}" ]; then
  IPFS_PATH=$PRV_REPO ipfs cat "$CID_PRV" >/dev/null 2>&1 && echo "  privato cat: OK" || echo "  privato cat: FALLITO"
fi
rm -f "$TESTFILE"

echo
echo "=== swarm key fingerprint (per confronto tra nodi) ==="
if [ -f "$PRV_REPO/swarm.key" ]; then
  md5sum "$PRV_REPO/swarm.key" | awk '{print "  privato swarm.key md5:", $1}'
  echo "  (deve COINCIDERE su tutti i nodi della rete privata)"
fi
