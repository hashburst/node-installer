#!/bin/bash
# Aggiorna /etc/hashburst/list.json dal database originale via download_list.php.
# L'IP del nodo 0 e' in whitelist per questa chiamata.
# Il token va tenuto in /etc/hashburst/download.token (permessi 600), NON qui.
set -e
TOKEN_FILE="${HB_DOWNLOAD_TOKEN_FILE:-/etc/hashburst/download.token}"
DEST="${HB_LIST_JSON_PATH:-/etc/hashburst/list.json}"
TMP="$(mktemp)"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "ERRORE: token file assente: $TOKEN_FILE" >&2
  echo "Crea il file con: echo -n 'IL_TUO_TOKEN' > $TOKEN_FILE && chmod 600 $TOKEN_FILE" >&2
  exit 1
fi
TOKEN="$(cat "$TOKEN_FILE")"

HTTP=$(curl -s -w "%{http_code}" -X POST https://api.synapta.net/download_list.php \
  -H "Content-Type: application/json" \
  -d "{\"token\": \"$TOKEN\"}" \
  -o "$TMP")

if [ "$HTTP" != "200" ]; then
  echo "ERRORE: download_list.php ha risposto HTTP $HTTP" >&2
  echo "Contenuto: $(head -c 200 "$TMP")" >&2
  rm -f "$TMP"
  exit 1
fi

# valida che sia JSON con entry apikey prima di sovrascrivere
if ! python3 -c "
import json,sys
d=json.load(open('$TMP'))
entries = d if isinstance(d,list) else d.get('users',d.get('entries',[]))
assert len(entries)>0, 'lista vuota'
assert all('apikey' in e for e in entries if isinstance(e,dict)), 'entry senza apikey'
print(f'  validato: {len(entries)} entry')
"; then
  echo "ERRORE: risposta non valida, list.json NON aggiornato" >&2
  rm -f "$TMP"
  exit 1
fi

# sovrascrittura atomica
chmod 600 "$TMP"
mv "$TMP" "$DEST"
echo "list.json aggiornato: $DEST"
