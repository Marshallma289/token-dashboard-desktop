"""Opt-in translation of skill descriptions with an on-device cache."""

from contextlib import closing
import hashlib
from http.client import HTTPSConnection, HTTPException
import json
import os
import re
import sqlite3
from threading import Lock

from skills_core import SkillError, plain


MAX_TEXTS = 32
MAX_TEXT = 2000
MAX_RESPONSE = 512 * 1024
_CACHE_LIMIT = 4096
_HAN = re.compile(r"[㐀-鿿U00020000-U000323af]")
_KANA = re.compile(r"[぀-ヿ]")


def auth_key():
    value = os.environ.get("DEEPL_AUTH_KEY", "").strip()
    return value if re.fullmatch(r"[\x21-\x7e]{1,512}", value) else ""


def _body(texts):
    return json.dumps({"text": texts, "target_lang": "ZH-HANS"}, ensure_ascii=False).encode()


def _batches(texts):
    batch = []
    for text in texts:
        if batch and len(_body([*batch, text])) > 128 * 1024:
            yield batch
            batch = []
        batch.append(text)
    if batch:
        yield batch


def _request(key, texts):
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    body = _body(texts)
    try:
        with closing(HTTPSConnection(host, timeout=10)) as connection:
            connection.request("POST", "/v2/translate", body=body, headers={
                "Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status == 403:
                raise SkillError("translation_auth", "DeepL rejected the API key")
            if response.status == 456:
                raise SkillError("translation_quota", "DeepL translation quota was reached")
            if response.status != 200:
                raise SkillError("translation_unavailable", "DeepL translation is temporarily unavailable")
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("DeepL response too large")
        translations = [item["text"] for item in json.loads(raw)["translations"]]
        if len(translations) != len(texts) or any(
                not isinstance(item, str) or not 0 < len(item.strip()) <= 4 * MAX_TEXT for item in translations):
            raise ValueError("Invalid DeepL response")
        return translations
    except SkillError:
        raise
    except (HTTPException, OSError, ValueError, TypeError, KeyError) as exc:
        raise SkillError("translation_unavailable", "DeepL translation is temporarily unavailable") from exc


def _needs_translation(text):
    han = len(_HAN.findall(text))
    letters = sum(char.isalpha() for char in text)
    return letters > 0 and (not han or han * 3 < letters or bool(_KANA.search(text)))


class TranslationCache:
    def __init__(self, data_dir):
        self.path = data_dir / "skill-translations.sqlite3"
        self._lock = Lock()

    def translate(self, lib, payload):
        texts = payload.get("texts")
        if (set(payload) != {"texts"} or not isinstance(texts, list) or len(texts) > MAX_TEXTS
                or any(not isinstance(text, str) or len(text) > MAX_TEXT for text in texts)):
            raise SkillError("invalid_payload", "Invalid description batch")
        if not lib.automation()["translate"]:
            return {"translations": {}}
        keys = {text: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for text in texts if _needs_translation(text)}
        if not keys:
            return {"translations": {}}
        # Translation runs outside the skills service lock. An overlapping UI
        # request waits here and looks up the result instead of billing it twice.
        with self._lock:
            try:
                return self._translate(keys)
            except sqlite3.Error as exc:
                raise SkillError("io_error", "Could not read the local translation cache") from exc

    def _translate(self, keys):
        for suffix in ("", "-journal", "-wal", "-shm"):
            st = plain(self.path.with_name(self.path.name + suffix), file=True, missing=True)
            if st is not None and st.st_nlink != 1:
                raise SkillError("unsafe_path", "Translation cache must not be hard-linked")
        placeholders = ",".join("?" for _ in keys)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS translation (digest TEXT UNIQUE, text TEXT NOT NULL)")
            cached = dict(db.execute(f"SELECT digest, text FROM translation WHERE digest IN ({placeholders})",
                                     tuple(keys.values())))
            translations = {text: cached[digest] for text, digest in keys.items() if digest in cached}
            missing = [text for text in keys if text not in translations]
            if not missing:
                return {"translations": translations}
            key = auth_key()
            if not key:
                return {"translations": translations, "error": "translation_key_missing"}
            error = {}
            for batch in _batches(missing):
                try:
                    results = dict(zip(batch, _request(key, batch), strict=True))
                except SkillError as exc:
                    error["error"] = exc.code
                    break
                db.executemany("INSERT OR IGNORE INTO translation VALUES (?, ?)",
                               [(keys[text], value) for text, value in results.items()])
                translations.update(results)
            db.execute("DELETE FROM translation WHERE rowid IN "
                       "(SELECT rowid FROM translation ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (_CACHE_LIMIT,))
            return {"translations": translations, **error}
