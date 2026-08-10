/* ============================================================================
 * HB-Files — Client-side crypto (Strato B2)
 * ----------------------------------------------------------------------------
 * Cifratura SOVRANA lato browser. Il server non vede mai ne' i file in chiaro
 * ne' le chiavi: riceve solo blob cifrati e un keystore opaco.
 *
 * SCHEMA (envelope encryption):
 *   - Data Key (DK): 256 bit casuali, cifra OGNI file con AES-GCM (IV per file)
 *   - la DK e' "avvolta" (wrapped) due volte nel keystore:
 *       - con KEK derivata dalla PASSWORD    (PBKDF2-SHA256, 600k iter, salt_pw)
 *       - con KEK derivata dalla RECOVERY KEY (PBKDF2-SHA256, 600k iter, salt_rk)
 *   - il keystore (blob JSON opaco) sta sul server: inutile senza pw o recovery
 *
 * L'utente sblocca la DK con la password (uso quotidiano) OPPURE con la
 * recovery key (se dimentica la password). Perse entrambe = dati irrecuperabili.
 *
 * Nessuna dipendenza: solo Web Crypto API (crypto.subtle), presente in tutti
 * i browser moderni e in Node >= 15.
 * ========================================================================== */

(function (global) {
  'use strict';

  const PBKDF2_ITER = 600000;          // sopra il minimo OWASP 2025 (310k)
  const PBKDF2_HASH = 'SHA-256';
  const SALT_LEN = 16;                  // 128 bit
  const IV_LEN = 12;                    // 96 bit, standard per AES-GCM
  const KEY_BITS = 256;
  const subtle = global.crypto.subtle;

  // ---- utility base64 <-> bytes ------------------------------------------
  function b64enc(buf) {
    const b = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s);
  }
  function b64dec(str) {
    const s = atob(str);
    const b = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i);
    return b;
  }
  function rnd(n) { return global.crypto.getRandomValues(new Uint8Array(n)); }

  // ---- derivazione KEK da una passphrase (password o recovery key) -------
  async function deriveKEK(passphrase, salt) {
    const material = await subtle.importKey(
      'raw', new TextEncoder().encode(passphrase),
      { name: 'PBKDF2' }, false, ['deriveKey']
    );
    return subtle.deriveKey(
      { name: 'PBKDF2', salt: salt, iterations: PBKDF2_ITER, hash: PBKDF2_HASH },
      material,
      { name: 'AES-GCM', length: KEY_BITS },
      false, ['wrapKey', 'unwrapKey']
    );
  }

  // ---- recovery key: 256 bit casuali, formato leggibile HBRK-xxxx-... ----
  function generateRecoveryKey() {
    const bytes = rnd(20);  // 160 bit -> 32 char base32
    const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    let out = '';
    for (let i = 0; i < bytes.length; i++) {
      out += B32[bytes[i] & 31];
      out += B32[(bytes[i] >> 3) & 31];
    }
    // formato a gruppi: HBRK-XXXX-XXXX-XXXX-...
    const groups = out.match(/.{1,4}/g).slice(0, 8);
    return 'HBRK-' + groups.join('-');
  }

  // ---- crea un nuovo keystore (primo setup dell'utente) ------------------
  // Genera una DK casuale, la avvolge con password e recovery key.
  // Ritorna { keystore, recoveryKey } — la recoveryKey va mostrata UNA volta.
  async function createKeystore(password) {
    const dk = await subtle.generateKey(
      { name: 'AES-GCM', length: KEY_BITS }, true, ['encrypt', 'decrypt']
    );
    const recoveryKey = generateRecoveryKey();

    const saltPw = rnd(SALT_LEN);
    const saltRk = rnd(SALT_LEN);
    const kekPw = await deriveKEK(password, saltPw);
    const kekRk = await deriveKEK(recoveryKey, saltRk);

    const ivPw = rnd(IV_LEN);
    const ivRk = rnd(IV_LEN);
    const wrapPw = await subtle.wrapKey('raw', dk, kekPw, { name: 'AES-GCM', iv: ivPw });
    const wrapRk = await subtle.wrapKey('raw', dk, kekRk, { name: 'AES-GCM', iv: ivRk });

    const keystore = {
      v: 1,
      iter: PBKDF2_ITER,
      pw: { salt: b64enc(saltPw), iv: b64enc(ivPw), wrap: b64enc(wrapPw) },
      rk: { salt: b64enc(saltRk), iv: b64enc(ivRk), wrap: b64enc(wrapRk) },
    };
    return { keystore, recoveryKey };
  }

  // ---- sblocca la DK dal keystore, con password O recovery key -----------
  async function unlockDataKey(keystore, passphrase, which) {
    // which = 'pw' oppure 'rk'
    const part = keystore[which];
    if (!part) throw new Error('keystore: sezione mancante ' + which);
    const salt = b64dec(part.salt);
    const iv = b64dec(part.iv);
    const wrap = b64dec(part.wrap);
    const kek = await deriveKEK(passphrase, salt);
    try {
      return await subtle.unwrapKey(
        'raw', wrap, kek, { name: 'AES-GCM', iv: iv },
        { name: 'AES-GCM', length: KEY_BITS }, true, ['encrypt', 'decrypt']
      );
    } catch (e) {
      throw new Error('Sblocco fallito: passphrase errata o keystore corrotto');
    }
  }

  // ---- cambio password: ri-avvolge la DK esistente con nuova password ----
  // Richiede di aver gia' sbloccato la DK (con vecchia pw o recovery).
  async function rewrapPassword(keystore, dataKey, newPassword) {
    const saltPw = rnd(SALT_LEN);
    const kekPw = await deriveKEK(newPassword, saltPw);
    const ivPw = rnd(IV_LEN);
    const wrapPw = await subtle.wrapKey('raw', dataKey, kekPw, { name: 'AES-GCM', iv: ivPw });
    keystore.pw = { salt: b64enc(saltPw), iv: b64enc(ivPw), wrap: b64enc(wrapPw) };
    return keystore;
  }

  // ---- cifra un file (ArrayBuffer/Uint8Array) con la DK ------------------
  // Ritorna un Blob: [IV (12B)] || [ciphertext+tag]. Pronto per l'upload.
  async function encryptFile(dataKey, plainBytes) {
    const iv = rnd(IV_LEN);
    const ct = await subtle.encrypt({ name: 'AES-GCM', iv: iv }, dataKey, plainBytes);
    const out = new Uint8Array(IV_LEN + ct.byteLength);
    out.set(iv, 0);
    out.set(new Uint8Array(ct), IV_LEN);
    return out;
  }

  // ---- decifra un file scaricato (IV prepended) --------------------------
  async function decryptFile(dataKey, encBytes) {
    const data = new Uint8Array(encBytes);
    const iv = data.slice(0, IV_LEN);
    const ct = data.slice(IV_LEN);
    const pt = await subtle.decrypt({ name: 'AES-GCM', iv: iv }, dataKey, ct);
    return new Uint8Array(pt);
  }

  const HBCrypto = {
    createKeystore, unlockDataKey, rewrapPassword,
    encryptFile, decryptFile, generateRecoveryKey,
    _internal: { deriveKEK, b64enc, b64dec },
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = HBCrypto;
  else global.HBCrypto = HBCrypto;

})(typeof globalThis !== 'undefined' ? globalThis : this);
