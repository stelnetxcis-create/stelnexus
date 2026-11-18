# Store v2.1 (apply on top of v2, or instead of it: this archive contains every changed file)

- FIX: the fight view still linked store.css?v=5, so browsers mixed the new chat script with the old cached CSS
  (reversed order "test : admin ADMIN LUX 12:48", broken sizing). Now v=7.
- Lux VIP chat: the name itself flashes (animated gold-to-rose shimmer + glow), light sweeps across the row, the Lux crest pulses.
  Disabled for people who prefer reduced motion.
- Layout now follows the width of the video area (container query), not the browser window: when the video area is
  under 560px (half-size window, phone) the chat stacks under the video as a smaller panel (S/M/L = ~22/32/46% of screen height).
- Account type tags (Admin, Master Agent...) and role colours in chat are hidden by default. Admin Variables -> Store ->
  "Show account type in livestream chat" turns them on.
- Seeing who is behind an alias is now limited to Admin and Pawn accounts (constant ALIAS_REVEAL_ROLES in app/services/store.py).
