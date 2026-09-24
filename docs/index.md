# AIRelays Docs

AIRelays is a local OpenAI-shaped relay with provider-scoped runtimes.

- OpenAI runtime: first-class ChatGPT subscription path
- Claude runtime: optional local text adapter through the `claude` CLI

Install the headless relay and CLI (macOS, Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-headless.sh | bash
```

Install the desktop tray app (macOS Apple Silicon, Linux x86_64):

```bash
curl -fsSL https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-desktop.sh | bash
```

On Windows (x64), run `irm https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-desktop.ps1 | iex`
in PowerShell. See the repository README for details.

Start with:

- [Getting Started](getting-started.md)
- [Configuration](configuration.md)
- [Security](security.md)
- [API Notes](api.md)
