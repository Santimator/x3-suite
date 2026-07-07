# secrets/

API keys live here and are **gitignored**. Nothing in this directory is
committed except this README.

To use the headless runner with NVIDIA NIM:

1. Get a free key at <https://build.nvidia.com> (it looks like `nvapi-...`).
2. Save it to `nim.key` in this directory — the key only, no quotes, no newline
   noise:

   ```
   printf '%s' 'nvapi-YOURKEYHERE' > nim.key
   ```

3. `config.json` points at it via `"api_key_file": "secrets/nim.key"`.

Alternatively, skip the file and export `NVIDIA_API_KEY` in your shell; the
runner falls back to that environment variable.
