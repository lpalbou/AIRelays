# FAQ

## Does AIRelays use my OpenAI platform API key?

No. The OpenAI runtime uses an AIRelays-owned ChatGPT subscription login.

## What token do I give my client?

Use the AIRelays relay token from `airelays init`, `airelays token show`, or `airelays token rotate`.

## Is AIRelays affiliated with any provider?

No. AIRelays is an independent third-party project.

## Can I disable relay auth?

Yes. Open local relay mode disables the AIRelays client-token gate.

```bash
airelays init --no-auth
airelays serve --no-auth --port 8080
```

## Which providers does AIRelays support?

The OpenAI subscription runtime only. No other providers are supported.

## Why do token-limit parameters return `422` on the OpenAI runtime?

The verified OpenAI subscription backend does not currently accept those fields on AIRelays’ OpenAI-shaped text-generation routes, so AIRelays rejects them explicitly.

## Why did I get `401` and then `429`?

The relay token was missing or wrong, and repeated bad attempts triggered the temporary IP block.
