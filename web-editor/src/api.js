/** The server side of the editor (ADR-0011): it decides validity, we only draw the answer. */

const cache = new Map(); // one fetch per page for the things that don't change while you type

async function getJson(path) {
  if (!cache.has(path)) {
    cache.set(
      path,
      fetch(path, { headers: { accept: "application/json" } }).then((r) => {
        if (!r.ok) throw new Error(`${path} answered ${r.status}`);
        return r.json();
      }),
    );
  }
  return cache.get(path);
}

export const language = () => getJson("/api/v1/language");
export const commands = () => getJson("/api/v1/commands");

async function post(path, body, signal) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

export const parse = (body, signal) => post("/api/v1/parse", body, signal);
export const explain = (body, signal) => post("/api/v1/explain", body, signal);
