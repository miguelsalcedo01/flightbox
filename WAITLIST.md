# Cloud waitlist — upgrading from mailto to real capture

The pricing page has a waitlist form. Today it opens the visitor's mail client with the
message prefilled (`site/index.html`, the "Cloud waitlist" IIFE). That always reaches a
real inbox and stores nothing, but it loses anyone who won't switch apps.

To capture signups server-side instead, three steps. Only step 1 needs a human — the
Cloudflare OAuth token wrangler uses locally can read/write KV *data* but cannot *create
namespaces* (`wrangler kv namespace create` fails with `Authentication error [code: 10000]`).

## 1. Create the namespace (dashboard, ~30s)

Cloudflare dashboard → **Storage & Databases → KV → Create namespace** → name it
`flightbox_waitlist`. Copy the namespace ID.

Then bind it to the Pages project: **Workers & Pages → flightbox → Settings → Bindings →
Add → KV namespace**, variable name `WAITLIST`, pointing at that namespace.

## 2. Add the function

Create `site/functions/api/waitlist.js`:

```js
export async function onRequestPost({ request, env }) {
  const json = (body, status) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });

  let email;
  try {
    ({ email } = await request.json());
  } catch {
    return json({ error: "bad request" }, 400);
  }

  if (typeof email !== "string" || !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email) || email.length > 254) {
    return json({ error: "invalid email" }, 400);
  }

  // key by email so a double submit overwrites rather than duplicating
  await env.WAITLIST.put(
    `wl:${email.toLowerCase()}`,
    JSON.stringify({
      email,
      at: new Date().toISOString(),
      ref: request.headers.get("referer") || null,
      country: request.headers.get("cf-ipcountry") || null,
    }),
  );

  return json({ ok: true }, 200);
}
```

Pages picks up `site/functions/` automatically on `wrangler pages deploy site` — no config
file needed.

## 3. Point the form at it

In `site/index.html`, replace the `window.location.href = 'mailto:...'` branch with:

```js
fetch('/api/waitlist', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ email: email })
}).then(function (r) {
  if (!r.ok) throw new Error('failed');
  msg.className = 'wl-msg ok';
  msg.textContent = '✓ YOU’RE ON THE LIST';
  input.value = '';
}).catch(function () {
  msg.className = 'wl-msg err';
  msg.textContent = 'COULDN’T SAVE THAT — EMAIL MIGUEL.SALCEDO01@GMAIL.COM INSTEAD';
});
```

Also update the `.wl-hint` copy — the current wording promises that nothing is collected,
which stops being true the moment the server stores the address.

## Reading the list

```bash
npx wrangler kv key list --binding WAITLIST --prefix wl:
```

## Note on the free tier

KV free tier covers 100k reads and 1k writes per day — far beyond what a waitlist needs.
