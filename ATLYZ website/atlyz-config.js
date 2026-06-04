/* ════════════════════════════════════════════════════════════════════
   ATLYZ — Frontend config (single source of truth)

   The website (this folder) is hosted on Cloudflare Pages at atlyz.com.
   The chatbot backend (Flask) runs on Railway. Because they live on
   different domains, every page that calls the backend must point at the
   backend URL — NOT at its own origin.

   To change the backend URL (e.g. after pointing api.atlyz.com at Railway),
   edit PROD_API below in this ONE place.
   ════════════════════════════════════════════════════════════════════ */
(function () {
  // Production backend (Railway). Switch to 'https://api.atlyz.com' once DNS is set up.
  var PROD_API = 'https://app.atlyz.com';

  var host    = window.location.hostname;
  var isLocal = window.location.protocol === 'file:' ||
                host === 'localhost' || host === '127.0.0.1';

  // Used by signup/setup/contact for API calls, and by the embed-code builders.
  window.ATLYZ_API = isLocal ? 'http://localhost:5002' : PROD_API;
})();
