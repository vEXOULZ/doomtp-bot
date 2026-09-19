/**
 * `<dtb-editor>` — the expression editor (ADR-0011).
 *
 * It upgrades a plain `<textarea>` it wraps, so a page works without JavaScript and the form still posts
 * the same field. Colours come from the local lexer; every error, completion and preview comes from the
 * server, which is the only authority on what the expression means.
 *
 *   <dtb-editor context="body" channel="doomtp" explain>
 *     <textarea name="expr">echo hello</textarea>
 *   </dtb-editor>
 */

import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, keymap, placeholder as placeholderText } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import { linter, lintKeymap, lintGutter } from "@codemirror/lint";
import { highlighting } from "./highlight.js";
import { theme } from "./theme.js";
import { completions } from "./complete.js";
import * as api from "./api.js";

const DEBOUNCE_MS = 300; // ADR-0011: diagnostics about a third of a second after typing stops
const EXPLAIN_MAX_CHARS = 500; // above this, explain is on demand only

class DoomtpEditor extends HTMLElement {
  connectedCallback() {
    if (this.view) return;
    this.textarea = this.querySelector("textarea");
    this.options = {
      context: this.getAttribute("context") || "body",
      channel: this.getAttribute("channel") || null,
      prefix: this.getAttribute("prefix") || undefined,
    };
    const host = document.createElement("div");
    host.className = "dtb-editor";
    this.appendChild(host);
    this.report = document.createElement("div");
    this.report.className = "dtb-report";
    this.report.hidden = true;
    this.appendChild(this.report);
    if (this.textarea) this.textarea.hidden = true;

    this.prefixOption = new Compartment();
    this.view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: this.textarea ? this.textarea.value : this.getAttribute("value") || "",
        extensions: [
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap, ...completionKeymap, ...lintKeymap]),
          EditorView.lineWrapping,
          placeholderText(this.getAttribute("hint") || ""),
          lintGutter(),
          highlighting(this.options),
          theme,
          autocompletion({ override: [completions(this.options)] }),
          linter((view) => this.diagnose(view), { delay: DEBOUNCE_MS }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) this.synced();
          }),
        ],
      }),
    });
    if (this.hasAttribute("explain")) this.addExplainButton();
  }

  get value() {
    return this.view ? this.view.state.doc.toString() : "";
  }

  /** Keep the original textarea current, so a plain form submit still posts what is on screen. */
  synced() {
    if (this.textarea) this.textarea.value = this.value;
    this.dispatchEvent(new CustomEvent("dtb-change", { detail: { value: this.value } }));
  }

  /** Server diagnostics. A parse error carries the offset the chat user would see as a column. */
  async diagnose(view) {
    const text = view.state.doc.toString();
    if (!text.trim()) {
      this.show(null);
      return [];
    }
    let answer;
    try {
      answer = await api.parse({ text, context: this.options.context, channel: this.options.channel });
    } catch {
      return []; // the bot is restarting, or the LAN blinked: colours stay, diagnostics wait
    }
    if (answer.ok) {
      this.show(null);
      if (this.hasAttribute("explain") && text.length <= EXPLAIN_MAX_CHARS) this.explain();
      return [];
    }
    const error = answer.error || {};
    const from = Math.min(error.offset ?? 0, Math.max(text.length - 1, 0));
    this.show(null);
    return [
      {
        from,
        to: Math.min(from + 1, text.length),
        severity: "error",
        source: error.code,
        message: error.hint ? `${error.message} — ${error.hint}` : error.message,
      },
    ];
  }

  addExplainButton() {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Explain";
    button.className = "dtb-explain";
    button.addEventListener("click", () => this.explain());
    this.appendChild(button);
  }

  /** The same report `!explain` prints, without running anything (spec §9). */
  async explain() {
    const text = this.value;
    if (!text.trim()) return;
    this.pending?.abort();
    this.pending = new AbortController();
    try {
      const report = await api.explain(
        { text, context: this.options.context, channel: this.options.channel },
        this.pending.signal,
      );
      this.show(report);
    } catch (error) {
      if (error.name !== "AbortError") this.show(null);
    }
  }

  show(report) {
    if (!report || report.parse_error) {
      this.report.hidden = true;
      this.report.textContent = "";
      return;
    }
    const lines = report.invocations.map((inv) =>
      inv.allowed
        ? `${inv.index}: ${inv.name} — ${inv.source}${inv.owner ? ` by ${inv.owner}` : ""}`
        : `${inv.index}: ${inv.name} — ${inv.reason}`,
    );
    for (const store of report.stores) {
      const how = store.append ? "appends to" : "stores in";
      lines.push(`${how} ${store.variable}${store.allowed ? "" : " — not allowed here"}`);
    }
    if (report.failure) lines.push(`stops at ${report.failed_index}: ${report.failure.message}`);
    if (report.would_send) lines.push(`sends: ${report.would_send}`);
    this.report.textContent = lines.join("\n");
    this.report.hidden = lines.length === 0;
  }

  disconnectedCallback() {
    this.pending?.abort();
    this.view?.destroy();
    this.view = null;
  }
}

export function register() {
  if (!customElements.get("dtb-editor")) customElements.define("dtb-editor", DoomtpEditor);
}
