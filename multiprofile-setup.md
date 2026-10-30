# Running WhatsApp Agent Platform Across Multiple Hermes Profiles

This guide explains how to install and operate the `whatsapp-agent-platform` plugin on secondary profiles within a multi-profile Hermes deployment.

---

## Architectural Overview

* **Multiplexed Ingress by Default:** Hermes manages platform ingress across profiles using a single host gateway process. You do not need to install or run a separate gateway daemon per profile under standard multi-profile operations.
* **Profile-Scoped State & Secrets:** Each profile maintains its own configuration file, `.env` file, and plugins directory under `~/.hermes/profiles/<profile-name>/`.
* **Distinct Agent Identities:** Every profile running the plugin requires its own dedicated Meta Agent credentials (`WHATSAPP_AGENT_PLATFORM_API_KEY`). Only one poller process may poll a single API key at a time.

---

## 1. Plugin Installation

### Option A: Fresh Installation on a New Profile

To install the plugin directly into a profile that does not yet contain it:

```bash
hermes -p <profile-name> plugins install whatsapp-agent-platform
```

During installation, Hermes interactively prompts for:
1. `WHATSAPP_AGENT_PLATFORM_API_KEY` (saved directly to `~/.hermes/profiles/<profile-name>/.env`).
2. Plugin enablement confirmation (`[y/N]`).
3. Python package manager dependency resolution (`[y/N]`).

---

### Option B: Handling Cloned or Copied Profiles

If you created your secondary profile by copying or cloning an existing profile directory, the plugin repository will already exist at `~/.hermes/profiles/<profile-name>/plugins/whatsapp-agent-platform`.

Attempting to run `plugins install` directly in this state outputs:

```text
Error: Plugin 'whatsapp-agent-platform' already exists. Use force reinstall or run `hermes plugins update whatsapp-agent-platform`.
```

> **Note on `plugins update`:** Although the CLI error suggests running `hermes plugins update`, doing so does **not** configure the new profile. It only checks catalog commit pins and exits because the copied files are already at the pinned commit. It will not prompt for new credentials or set up profile-specific dependencies.

#### The Clean Reinstall Path

The working path to initialize the plugin on a copied profile is to remove the pre-existing plugin directory and perform a clean install so that Hermes runs the interactive credential and dependency wizard:

```bash
hermes -p <profile-name> plugins remove whatsapp-agent-platform
hermes -p <profile-name> plugins install whatsapp-agent-platform
```

During the install prompt:
1. Enter the new profile's dedicated `WHATSAPP_AGENT_PLATFORM_API_KEY`.
2. Confirm plugin enablement (`Y`).
3. Allow Hermes to prepare the Python dependencies with the package manager (`Y`).

---

## 2. Verification & Troubleshooting

After completing the installation, verify that the profile is picked up by the host multiplexer and processing traffic:

1. **Restart the Host Gateway:**
   Restart the gateway from the primary profile so the host multiplexer discovers the new profile's credentials:
   ```bash
   hermes -p default gateway restart
   ```

2. **Check Profile Gateway Status:**
   Verify that the profile is recognized and served by the shared ingress:
   ```bash
   hermes -p <profile-name> gateway status
   ```

   Expected output:
   ```text
   ✓ Gateway is running via the default-profile multiplexer
   Inbound callback URLs on the shared listener:
     api_server: http://127.0.0.1:8642/p/<profile-name>/v1
   ```

3. **Inspect Gateway Logs:**
   Ensure long-polling connects to Meta's `/agent/v1` without authentication errors:
   ```bash
   journalctl --user -u hermes-gateway -n 50 -f
   ```

4. **Validate Agent Ingress:**
   Send a text message from WhatsApp to the agent corresponding to this profile's API key to verify that messages route cleanly to the session.