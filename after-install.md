# WhatsApp Agent Platform

1. In WhatsApp: **Settings → Agents → Create an agent**, then open the agent's chat → **Chat info → API key**.
2. `hermes plugins enable whatsapp-agent-platform` (if not enabled during install) and set
   `WHATSAPP_AGENT_PLATFORM_API_KEY` (or run `hermes gateway setup`).
3. Recommended — the platform can't edit messages and allows 12 sends/minute:

   ```yaml
   display:
     platforms:
       whatsapp_agent_platform:
         tool_progress: "off"
         interim_assistant_messages: false
         long_running_notifications: false
         busy_ack_detail: false
   ```
4. Restart the gateway and message your agent. Only one process may poll a key at a time.

Only the agent's creator can talk to Hermes: the plugin confirms the creator with Meta before dispatching a
message. Per WhatsApp's Third-Party Agents Terms, agent chats are **not end-to-end encrypted**: messages pass
through Meta to this Hermes instance.
