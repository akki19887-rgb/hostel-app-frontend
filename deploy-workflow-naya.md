Ye file sirf copy karne ke liye hai. Website par nahi jaati (firebase.json
me `**/*.md` ignore hai).

Neeche wala poora text copy karke `.github/workflows/firebase-deploy.yml`
me paste karna hai - purana sab mita kar.

Isme ek hi nayi baat hai: deploy ka error ab run ke **Summary** page par
seedha chhap jayega, taaki phone par bhi bina koi step khole padha ja sake.
Saath me ye bhi chhapega ki chaabi kis service account ki hai - taaki pata
chale ki role sahi wale account ko diya gaya ya nahi.

----------------------------------------------------------------------

name: Deploy to Firebase Hosting

on:
  push:
    branches:
      - main
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-24.04
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Setup Node
        uses: actions/setup-node@v4
        with:
          node-version: '22'

      - name: Check service account key
        env:
          SA: ${{ secrets.FIREBASE_SERVICE_ACCOUNT }}
        run: |
          printf '%s' "$SA" > "$RUNNER_TEMP/sa.json"
          node -e "const j=require(process.env.RUNNER_TEMP+'/sa.json');console.log('project_id:',j.project_id);console.log('client_email:',j.client_email)" | tee "$RUNNER_TEMP/who.log"
          {
            echo '## Chaabi kiski hai'
            echo ''
            echo '```'
            cat "$RUNNER_TEMP/who.log"
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"

      - name: Install Firebase CLI
        run: npm install -g firebase-tools

      - name: Deploy to Firebase Hosting
        env:
          GOOGLE_APPLICATION_CREDENTIALS: ${{ runner.temp }}/sa.json
        run: |
          set +e
          firebase deploy --only hosting --project hostel-om --non-interactive 2>&1 | tee "$RUNNER_TEMP/deploy.log"
          code=${PIPESTATUS[0]}
          {
            echo '## Deploy ka natija'
            echo ''
            echo "exit code: $code"
            echo ''
            echo '```'
            tail -n 40 "$RUNNER_TEMP/deploy.log"
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"
          exit $code
