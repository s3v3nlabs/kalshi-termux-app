# Kalshi API keys

This app uses Kalshi API-key authentication for Account endpoints and live trading. A key consists of a **Key ID** and a private key PEM. Kalshi creates **Ed25519** keys by default; **RSA** keys also work. The app detects the type from the key itself. Public market scanning does not need a key.

## Create a key

1. Sign in at [kalshi.com/account/profile](https://kalshi.com/account/profile).
2. Open **profile/account → API Keys**.
3. Choose **Create New API Key**.
4. Save the displayed **Key ID** and download the PEM immediately. The private key is shown/downloadable only once; it cannot be retrieved later.
5. Keep the PEM private. Do not paste it into GitHub, an issue, a log, or chat.

## Add it to this app

### Settings UI (recommended)

1. Open **Settings**.
2. Paste the **Kalshi Key ID** and the **private key text**. The box accepts both `BEGIN RSA PRIVATE KEY` and `BEGIN PRIVATE KEY`.
3. Enter the **app lock password** (this is not your Kalshi login) and choose **Save on this phone**.
4. Check `/api/status` or the Settings badge. A configured key is shown as **Saved on this phone**; after unlock it is **Unlocked**.

The PEM is stored locally at `secrets/kalshi.key` with restrictive permissions, while the Key ID is kept in the encrypted local vault. This app does not upload credentials to a third party.

### Local `.env` files

For a local-only setup, put the Key ID in an untracked `.env` and store the PEM at `secrets/kalshi.key`:

```dotenv
KALSHI_KEY_ID=<your-key-id>
KALSHI_KEY_PATH=secrets/kalshi.key
```

```bash
chmod 600 secrets/kalshi.key
```

Never replace the placeholders above with real credentials in a tracked file. `.env` and `secrets/*` are ignored; only `secrets/.gitkeep` is allowed as a repository placeholder.

## Rotate a key

1. Create a new key using the same UI path above.
2. Save the new Key ID and download its PEM immediately.
3. Replace the local credentials in Settings, or update your local `.env` and PEM file.
4. Restart if you changed `.env`, unlock the vault if needed, and verify Account access.
5. Only after the new key works, revoke the old key from the same **API Keys** list.

Because the private key is one-time, create a new key if the PEM was lost or exposed; do not wait for it to be recoverable.

## Delete or revoke a key

There are two separate actions:

- **Revoke remotely:** On [Kalshi](https://kalshi.com/account/profile), open **API Keys**, find the key, and choose **delete/revoke**. This prevents that key from authenticating with Kalshi.
- **Clear locally:** In this app, choose **Settings → API credentials → Clear local keys**. This deletes the local PEM and clears the runtime/vault Key ID only. It never makes a remote Kalshi delete/revoke request. If the Key ID is also in `.env`, remove it from that local file manually.

After revoking or clearing a key, `/api/status` should report `has_keys: false` when no other local Key ID and PEM are configured.

## Safety checklist

- GitHub clones never include Kalshi keys.
- Keep `.env`, PEM files, and vault/database files out of source control.
- Use **Save on this phone** and a strong app lock password where possible.
- Revoke old or exposed keys promptly in Kalshi's API Keys list.
