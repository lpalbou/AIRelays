# AIRelays Docs

AIRelays is a local OpenAI-shaped relay with provider-scoped runtimes.

- OpenAI runtime: ChatGPT subscription access with multi-account balancing, plus image generation and edits (`gpt-image-2`)
- Claude runtime: chat, completions and Responses with images, files, conversations and tool calls, balanced across AIRelays-owned `claude` CLI subscription profiles, separate from your personal Claude Code sign-in

![A compact matte-black device with a thin green light line and the word AIRelays engraved on top, photographed on a slate pedestal](assets/images/airelays-product-shot.jpg)

*Generated through AIRelays with `POST /v1/images/generations`; the request is
in [API Notes](api.md#image-examples).*

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
- [Capabilities](capabilities.md)
- [Configuration](configuration.md)
- [Security](security.md)
- [API Notes](api.md)
