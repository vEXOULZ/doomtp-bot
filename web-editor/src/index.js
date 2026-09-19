/** Bundle entry: define the element as soon as the script loads, and export the lexer for tests. */

import { register } from "./editor.js";

export { tokenize } from "./tokens.js";

register();
