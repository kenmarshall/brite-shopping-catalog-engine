Commit and push changes across all Brite Shopping repos.

Usage: /commit-all

Repos to check (in order):
1. **Catalog Engine**: `/Users/kennethmarshall/dev/brite_shopping/brite-shopping-catalog-engine`
2. **API**: `/Users/kennethmarshall/dev/brite_shopping/brite_shopping_api`
3. **Mobile**: `/Users/kennethmarshall/dev/brite_shopping/brite-shopping-mobile`

For each repo:
1. Run `git status` to check for changes
2. If there are changes, run `git diff --stat` to summarize
3. Stage relevant files (not .env, credentials, or node_modules)
4. Commit with a descriptive message following the repo's commit style
5. Push to main
6. Report what was committed

Skip repos with no changes. Quote paths containing parentheses (e.g., `"app/(tabs)/index.tsx"`).
