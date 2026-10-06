# secrets/

API keys live here and are **gitignored**. Nothing in this directory is
committed except this README.

Save the key for the provider `config.json` points at as `api.key` — the key
only, no quotes:

```
printf '%s' 'YOUR-KEY' > api.key
```

`config.json` finds it via `"api_key_file": "secrets/api.key"`. Alternatively,
skip the file and export `PDF2EPUB_API_KEY`; the runner falls back to it.
