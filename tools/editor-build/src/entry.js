// Entry point for the bundled editor asset (implementation plan §1.2).
// Everything the editor page needs is re-exported from here so that esbuild can
// produce a single IIFE global — no module loader in the browser.

export { EditorState } from "@codemirror/state";
export { EditorView, keymap } from "@codemirror/view";
export { basicSetup } from "codemirror";

// The three language modes. `css` and `javascript` are exported in their own
// right because they are what lets `html()` highlight embedded <style>/<script>.
export { html } from "@codemirror/lang-html";
export { css } from "@codemirror/lang-css";
export { javascript } from "@codemirror/lang-javascript";
