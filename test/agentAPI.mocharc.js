// Mocha configuration for agentAPI.js unit tests (no puppeteer)
module.exports = {
  spec: ['test/tests/agentAPITest.mjs'],
  timeout: '5000',
  ui: 'bdd',
  reporter: 'spec',
};
