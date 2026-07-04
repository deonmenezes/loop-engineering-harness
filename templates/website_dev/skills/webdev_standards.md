# Web Dev Standards
- Files are the deliverable: always write real files to the workspace, never paste code only into chat.
- Contract-first: frontend and backend must agree on routes and JSON shapes; when in doubt, read the other side's code instead of assuming.
- Accessibility floor: semantic elements, labels on inputs, alt text, keyboard-reachable interactions.
- Responsive by default: mobile layout first, then widen.
- Errors are UX: every fetch handles loading, empty, and error states.
- Verify before declaring done: run node --check / build / tests when available; otherwise trace imports and routes by hand and say exactly what was and wasn't verified.