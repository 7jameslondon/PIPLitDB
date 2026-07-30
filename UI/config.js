/*
 * Leave GitHub values blank when previewing locally. On a standard GitHub Pages
 * project URL, the owner and repository are detected automatically.
 *
 * Set owner and repository explicitly before using a custom domain or a Pages
 * layout where the repository name is not the first URL path segment.
 */
window.PIP_LITDB_CONFIG = Object.freeze({
  github: Object.freeze({
    owner: "",
    repository: "",
    ref: "",
  }),
  paths: Object.freeze({
    records: "database/records",
    vocabularies: "database/vocabularies",
  }),
  localPaths: Object.freeze({
    records: "../database/records/",
    vocabularies: "../database/vocabularies/",
  }),
  requestConcurrency: 8,
});
