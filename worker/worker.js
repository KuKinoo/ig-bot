// Cloudflare Worker: receives Telegram's webhook and hands each update to GitHub Actions.
// It does no real work itself, so it stays far inside the free plan (100,000 requests/day).
//
// Settings (Worker > Settings > Variables and Secrets):
//   WEBHOOK_SECRET      secret  any random string; the same value is given to Telegram in setWebhook
//   TELEGRAM_CHAT_ID    secret  your numeric chat id - everyone else is ignored
//   TELEGRAM_BOT_TOKEN  secret  used only to acknowledge button taps right away
//   GH_TOKEN            secret  fine-grained GitHub token for this repo only, permission "Contents: read and write"
//   GITHUB_REPO         text    "owner/repo"

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("ok");

    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const chatId = update.message?.chat?.id ?? update.callback_query?.message?.chat?.id;
    if (String(chatId) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ok"); // not the approver

    if (update.callback_query) {
      // Stop the spinner on the button immediately; the real answer arrives from GitHub Actions.
      ctx.waitUntil(
        fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ callback_query_id: update.callback_query.id, text: "Working on it…" }),
        }),
      );
    }

    ctx.waitUntil(dispatch(update, env));
    return new Response("ok");
  },
};

async function dispatch(update, env) {
  const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "ig-bot-relay",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      event_type: "telegram_update",
      client_payload: { update: JSON.stringify(update) },
    }),
  });
  if (!resp.ok) console.error("GitHub dispatch failed", resp.status, await resp.text());
}
