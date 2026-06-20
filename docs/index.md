# AIRelays

`AIRelays` is an independent local relay that exposes an OpenAI-compatible API over a subscription-backed upstream login. It stores its own upstream auth, can protect the relay with a local bearer token, and logs each request/response transit to hourly JSONL files.

## Choose A Launch Mode

Protected relay mode:

```bash
python -m pip install airelays
airelays init
airelays login
airelays serve --port 8080
```

Open local relay mode:

```bash
python -m pip install airelays
airelays init --no-auth
airelays login
airelays serve --no-auth --port 8080
```

Protected mode requires a relay token for `/v1/*` and `/no-tools/v1/*`.
Open mode removes that token check for the running process and is best kept on the default loopback listener.

## Start Here

- [Getting Started](getting-started.md)
  - install, login, launch modes, and first requests
- [Configuration](configuration.md)
  - config file, environment overrides, and token inputs
- [Security](security.md)
  - bearer auth, rate limits, and open local relay mode
- [API Notes](api.md)
  - supported routes and explicit compatibility boundaries
- [FAQ](faq.md)
  - client token usage, open mode, and common operational questions
