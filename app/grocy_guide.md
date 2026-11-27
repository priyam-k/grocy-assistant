# Grocy feature guide (Grocy 4.x)

Reference for answering "how does Grocy do X", "can Grocy do Y" and "where do I find Z". Menu names
are the English ones; the left sidebar holds the main pages and the wrench icon (top right) holds
settings. Official site and documentation: https://grocy.info, demo: https://demo.grocy.info.

## Stock

- **Stock overview**: every product in stock with amount, next due date and value. Filter by
  location, product group or status (due soon, overdue, expired, below minimum). Each row has quick
  buttons (consume the quick amount, consume all, mark one opened) and a menu for purchase, consume,
  transfer, inventory, add to shopping list, stock entries and the product's journal.
- **Purchase**: adds stock. Fields: product, amount and unit, due date, price (per unit or total),
  store, location, note. Works with a barcode scanner or the phone camera. An unknown barcode can
  be attached to an existing product or used to create a new one.
- **Consume**: removes stock, oldest/opened first by default, or from a chosen location or stock
  entry. "Spoiled" marks it as waste, which shows up in statistics. Can also "mark as opened".
- **Transfer**: moves stock between locations. Moving into a location marked as a freezer can
  extend the due date ("default due days after freezing"); moving out can shorten it (thawing).
- **Inventory**: you say how much is really there and Grocy books the difference.
- **Stock entries**: each purchase is its own batch with its own due date, price, location, opened
  state and note. Entries can be edited individually.
- **Stock journal**: every booking (purchase, consume, transfer, inventory, opened) with who did it
  and an undo button. "Journal summary" totals them per product and user.
- **Reports**: "Stock reports > Spendings" shows money spent per product group or store over time.
  "Location content sheet" is a printable list of what is in each location.

## Products (Manage master data > Products)

- **Due dates**: default due days (-1 = never goes off), default due days after opening, after
  freezing and after thawing. Due date type is either "best before" (after the date it is "overdue",
  still usable) or "expiration" (after the date it is "expired").
- **Minimum stock amount**: below it the product is flagged on the stock overview and can be added
  to the shopping list with one button. "Treat opened as out of stock" counts opened packs as gone.
- **Units**: each product has a stock unit, a purchase unit, a consume unit and a price unit.
  Conversions (for example 1 pack = 6 pieces) can be global or per product. The stock unit cannot
  be changed once the product has been purchased.
- **Parent and sub products**: a generic parent (Milk) with variants below it (brand A, brand B).
  A recipe that asks for the parent is satisfied by any variant.
- **Barcodes**: several per product, each optionally with its own amount, unit, store and price.
- **Other options**: default location and default consume location, default store, product group,
  picture, quick consume / quick open amount, energy (calories) per stock unit, tare weight
  handling (for containers weighed together with their content), "should not be frozen", hide from
  stock overview, and active/inactive.
- **Grocycode**: Grocy's own code for a product or a single stock entry; can be printed as a label
  (label printer support is a feature flag) and scanned back in.

## Other master data

Locations (with an "is freezer" switch), stores, quantity units and conversions, product groups,
chores, batteries, task categories. **Userfields** add custom fields to any of these, and
**userentities** are completely custom tables with their own menu entry.

## Shopping list

Several lists are possible. An item is a product with an amount and unit, or just a free-text
note. Items are grouped by product group. Buttons add everything below its minimum stock, add
overdue and expired products, clear the list, print it, or book all items into stock after
shopping. Items can be ticked off as done. Purchasing a product takes it off the list.

## Recipes and meal plan

- **Recipes**: ingredients with amounts, servings, preparation text and a picture. Grocy shows
  whether everything is in stock, how many ingredients are missing, the cost and the calories. One
  button consumes all ingredients, another puts the missing ones on the shopping list. An
  ingredient can be set to "only check if any amount is in stock" (salt, oil). Recipes can include
  other recipes. The "due score" ranks recipes that use up products that are due soon.
- **Meal plan**: a week view where recipes, single products or notes are placed on days, in
  sections such as breakfast, lunch and dinner. Shows the week's cost and what is missing, and can
  put the missing ingredients on the shopping list.

## Chores, tasks, batteries, equipment, calendar

- **Chores**: recurring jobs. Schedules: manually, hourly, daily, weekly, monthly, yearly, or
  "adaptive" (repeats N days after it was last done). Can be assigned to people in turn (whoever
  did it least, random, alphabetical) or to fixed people. Doing a chore can consume a product
  (for example a dishwasher tab).
- **Tasks**: one-off to-dos with an optional due date, category and assigned person.
- **Batteries**: tracks rechargeable batteries and when they were last charged, with a charge
  interval reminder.
- **Equipment**: a place to keep manuals (PDF) and notes for appliances.
- **Calendar**: shows due dates of products, chores, tasks, battery charges and the meal plan.
  It offers an iCal link so the same events appear in a phone calendar.

## Settings, users and setup

- **Wrench icon**: user settings (stock defaults such as "due soon" days and default location,
  shopping list and recipe settings), night mode, manage users, manage API keys, about.
- **Users**: several accounts, each with its own permissions and settings. The journal records
  which user made each booking.
- **Feature flags**: whole areas (shopping list, recipes, meal plan, chores, tasks, batteries,
  equipment, calendar) and details (price tracking, location tracking, due date tracking, opened
  tracking, freezing, label printer) can be switched off. They are set in `data/config.php`, or
  with environment variables named `GROCY_FEATURE_FLAG_...` when Grocy runs in Docker. Currency,
  language and first day of the week are set the same way.
- **Barcode lookup**: an optional plugin can fill in new products from Open Food Facts.
- **Backup**: everything lives in Grocy's data folder; the database is the SQLite file `grocy.db`.
- **Apps**: Grocy is a web app that works on phones; there are also third-party mobile apps.
- **API**: Grocy has a REST API (documented inside Grocy under the wrench menu's API browser).

## What this chat assistant can and cannot do

Through this chat:

- Stock: read it, add purchases, consume (also from one specific batch), mark opened, correct
  amounts, move between locations, edit a batch, undo bookings, see spending.
- Products: create and edit (shelf life, minimum stock, default location and store, group, parent
  product, best-before versus expiry, hide when no longer used), attach barcodes, add unit
  conversions.
- Shopping lists: several lists, add, change, tick off, move between lists, remove items, add
  everything that is low, overdue or expired, clear a list.
- Recipes: read, create, rename, change instructions and servings, add or change ingredients,
  cook (consume) for a number of servings, put missing ingredients on the list.
- Meal plan: read, add recipes, products or notes to a day and section, move, change, mark done.
- Tasks: add, edit, assign, complete, reopen. Chores: create, edit, retire, record or skip, undo.
- Batteries: list and record charges. Calendar: everything dated in a period.
- Lists Grocy is organised by: create and rename locations, stores, units, product groups,
  shopping lists, meal plan sections, task categories, batteries, equipment.

Only in Grocy itself: deleting anything other than shopping list items (products, recipes, recipe
ingredients, meal plan entries, tasks, chores), merging products, product pictures, scanning with
the camera, manuals for equipment, userfields, users and permissions, settings and feature flags,
printing.
