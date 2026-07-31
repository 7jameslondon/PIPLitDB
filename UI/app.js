(function () {
  "use strict";

  const config = window.PIP_LITDB_CONFIG;
  const KNOWN_RECORD_FIELDS = new Set([
    "document_type",
    "publication_stage",
    "title",
    "authors",
    "doi",
    "url",
    "publication_year",
    "journal",
    "related_papers",
    "pip_litdb_status",
    "pip_litdb_notes",
  ]);

  const state = {
    records: [],
    recordById: new Map(),
    vocabularies: {},
    source: null,
    isLoading: false,
    yearBounds: { min: null, max: null },
  };

  const ui = {};

  document.addEventListener("DOMContentLoaded", () => {
    captureElements();
    bindEvents();
    loadDatabase();
  });

  function captureElements() {
    ui.search = document.querySelector("#search");
    ui.documentType = document.querySelector("#document-type-filter");
    ui.publicationStage = document.querySelector("#publication-stage-filter");
    ui.yearRange = document.querySelector("#year-range-filter");
    ui.yearMinInput = document.querySelector("#year-min-input");
    ui.yearMaxInput = document.querySelector("#year-max-input");
    ui.yearSlider = document.querySelector("#year-range-slider");
    ui.yearMinSlider = document.querySelector("#year-min-slider");
    ui.yearMaxSlider = document.querySelector("#year-max-slider");
    ui.status = document.querySelector("#status-filter");
    ui.sort = document.querySelector("#sort");
    ui.clearFilters = document.querySelector("#clear-filters");
    ui.emptyClearFilters = document.querySelector("#empty-clear-filters");
    ui.retry = document.querySelector("#retry-load");
    ui.recordCount = document.querySelector("#record-count");
    ui.records = document.querySelector("#records");
    ui.loading = document.querySelector("#loading-state");
    ui.empty = document.querySelector("#empty-state");
    ui.emptyTitle = document.querySelector("#empty-title");
    ui.emptyDetail = document.querySelector("#empty-detail");
    ui.error = document.querySelector("#error-state");
    ui.errorTitle = document.querySelector("#error-title");
    ui.errorDetail = document.querySelector("#error-detail");
    ui.sourceDot = document.querySelector("#source-dot");
    ui.sourceName = document.querySelector("#source-name");
    ui.sourceLink = document.querySelector("#source-link");
    ui.dialog = document.querySelector("#record-dialog");
    ui.dialogId = document.querySelector("#dialog-id");
    ui.dialogContent = document.querySelector("#dialog-content");
    ui.dialogClose = document.querySelector("#dialog-close");
  }

  function bindEvents() {
    const filterControls = [
      ui.search,
      ui.documentType,
      ui.publicationStage,
      ui.status,
      ui.sort,
    ];

    filterControls.forEach((control) => {
      control.addEventListener(control === ui.search ? "input" : "change", renderDatabase);
    });

    [
      [ui.yearMinInput, "min"],
      [ui.yearMaxInput, "max"],
    ].forEach(([input, edge]) => {
      input.addEventListener("input", () => syncYearRangeFromNumber(edge, false));
      input.addEventListener("change", () => syncYearRangeFromNumber(edge, true));
    });

    [
      [ui.yearMinSlider, "min"],
      [ui.yearMaxSlider, "max"],
    ].forEach(([slider, edge]) => {
      slider.addEventListener("input", () => syncYearRangeFromSlider(edge, false));
      slider.addEventListener("change", () => syncYearRangeFromSlider(edge, true));
      slider.addEventListener("focus", () => bringYearSliderThumbToFront(slider));
      slider.addEventListener("pointerdown", () => bringYearSliderThumbToFront(slider));
    });

    ui.clearFilters.addEventListener("click", resetFilters);
    ui.emptyClearFilters.addEventListener("click", resetFilters);
    ui.retry.addEventListener("click", loadDatabase);
    ui.dialogClose.addEventListener("click", () => ui.dialog.close());
    ui.dialog.addEventListener("click", closeDialogFromBackdrop);
    ui.dialog.addEventListener("close", clearRecordHash);
    window.addEventListener("hashchange", openRecordFromHash);
  }

  async function loadDatabase() {
    if (state.isLoading) return;

    setLoadingState();

    try {
      if (!config) {
        throw new Error("UI configuration could not be loaded.");
      }
      if (!window.jsyaml || typeof window.jsyaml.load !== "function") {
        throw new Error(
          "The YAML reader could not be loaded. Check the internet connection and refresh the page.",
        );
      }

      const source = selectDataSource();
      const result = await source.load();

      state.records = result.records;
      state.recordById = new Map(result.records.map((record) => [record.pip_litdb_id, record]));
      state.vocabularies = result.vocabularies;
      state.source = result.source;

      populateFilters();
      setControlsDisabled(false);
      setSourceReady(result.source);
      state.isLoading = false;
      renderDatabase();
      openRecordFromHash();
    } catch (error) {
      console.error(error);
      setErrorState(error);
    } finally {
      state.isLoading = false;
    }
  }

  function selectDataSource() {
    const github = resolveGithubCoordinates();
    if (github) return createGithubSource(github);
    return createLocalSource();
  }

  function resolveGithubCoordinates() {
    const configuredOwner = config.github.owner.trim();
    const configuredRepository = config.github.repository.trim();

    if (configuredOwner && configuredRepository) {
      return {
        owner: configuredOwner,
        repository: configuredRepository,
        ref: config.github.ref.trim(),
      };
    }

    const hostname = window.location.hostname.toLowerCase();
    if (!hostname.endsWith(".github.io")) return null;

    const owner = hostname.split(".")[0];
    const pathParts = window.location.pathname.split("/").filter(Boolean);
    if (!owner || pathParts.length === 0) return null;

    return {
      owner,
      repository: decodeURIComponent(pathParts[0]),
      ref: config.github.ref.trim(),
    };
  }

  function createGithubSource(github) {
    const apiBase = `https://api.github.com/repos/${encodeURIComponent(github.owner)}/${encodeURIComponent(github.repository)}`;
    const webBase = `https://github.com/${encodeURIComponent(github.owner)}/${encodeURIComponent(github.repository)}`;

    return {
      async load() {
        const repository = await fetchJson(apiBase);
        const ref = github.ref || repository.default_branch;
        const commit = await fetchJson(`${apiBase}/commits/${encodeURIComponent(ref)}`);
        const tree = await fetchJson(`${apiBase}/git/trees/${commit.sha}?recursive=1`);

        if (tree.truncated) {
          throw new Error(
            "The repository file listing was too large to read completely. Configure a narrower record source.",
          );
        }

        const recordPrefix = `${trimSlashes(config.paths.records)}/`;
        const recordPaths = tree.tree
          .filter(
            (entry) =>
              entry.type === "blob" &&
              entry.path.startsWith(recordPrefix) &&
              /^\d{5}\.ya?ml$/i.test(entry.path.slice(recordPrefix.length)),
          )
          .map((entry) => entry.path)
          .sort();

        const rawBase = `https://raw.githubusercontent.com/${encodeURIComponent(github.owner)}/${encodeURIComponent(github.repository)}/${commit.sha}`;
        const [records, vocabularies] = await Promise.all([
          loadRecords(recordPaths, (path) => fetchText(`${rawBase}/${path}`, "force-cache")),
          loadVocabularies((name) =>
            fetchText(
              `${rawBase}/${trimSlashes(config.paths.vocabularies)}/${name}.yaml`,
              "force-cache",
            ),
          ),
        ]);

        return {
          records,
          vocabularies,
          source: {
            name: "GitHub",
            detail: `${commit.sha.slice(0, 7)} on ${ref}`,
            url: `${webBase}/tree/${commit.sha}/${trimSlashes(config.paths.records)}`,
          },
        };
      },
    };
  }

  function createLocalSource() {
    return {
      async load() {
        if (window.location.protocol === "file:") {
          throw new Error(
            "Browsers cannot discover database files from a file:// page. Serve the project root with a local web server, then open /UI/.",
          );
        }

        const directoryUrl = new URL(ensureTrailingSlash(config.localPaths.records), document.baseURI);
        const directoryHtml = await fetchText(directoryUrl.href, "no-store");
        const parser = new DOMParser();
        const directoryDocument = parser.parseFromString(directoryHtml, "text/html");
        const recordUrls = [...directoryDocument.querySelectorAll("a[href]")]
          .map((anchor) => new URL(anchor.getAttribute("href"), directoryUrl))
          .filter((url) => /^\d{5}\.ya?ml$/i.test(decodeURIComponent(url.pathname.split("/").pop())))
          .sort((a, b) => a.href.localeCompare(b.href));

        const vocabulariesUrl = new URL(
          ensureTrailingSlash(config.localPaths.vocabularies),
          document.baseURI,
        );

        const [records, vocabularies] = await Promise.all([
          loadRecords(
            recordUrls.map((url) => url.href),
            (url) => fetchText(url, "no-store"),
          ),
          loadVocabularies((name) =>
            fetchText(new URL(`${name}.yaml`, vocabulariesUrl).href, "no-store"),
          ),
        ]);

        return {
          records,
          vocabularies,
          source: {
            name: "Local files",
            detail: "Read on this refresh",
            url: directoryUrl.href,
          },
        };
      },
    };
  }

  async function loadRecords(paths, readRecord) {
    const records = await mapWithConcurrency(
      paths,
      Math.max(1, Number(config.requestConcurrency) || 8),
      async (path) => {
        const fileName = decodeURIComponent(path.split("/").pop());
        const idMatch = fileName.match(/^(\d{5})\.ya?ml$/i);
        if (!idMatch) throw new Error(`Invalid record filename: ${fileName}`);

        let record;
        try {
          record = window.jsyaml.load(await readRecord(path));
        } catch (error) {
          throw new Error(`${fileName} could not be parsed: ${error.message}`);
        }

        if (!record || typeof record !== "object" || Array.isArray(record)) {
          throw new Error(`${fileName} does not contain a YAML object.`);
        }

        return {
          ...record,
          pip_litdb_id: idMatch[1],
        };
      },
    );

    return records.sort((a, b) => a.pip_litdb_id.localeCompare(b.pip_litdb_id));
  }

  async function loadVocabularies(readVocabulary) {
    const names = ["document-types", "publication-stages", "record-statuses", "relationship-types"];
    const entries = await Promise.all(
      names.map(async (name) => {
        try {
          const parsed = window.jsyaml.load(await readVocabulary(name));
          return [name, parsed && typeof parsed === "object" ? parsed : {}];
        } catch (error) {
          throw new Error(`${name}.yaml could not be loaded: ${error.message}`);
        }
      }),
    );
    return Object.fromEntries(entries);
  }

  async function mapWithConcurrency(items, concurrency, mapper) {
    const results = new Array(items.length);
    let nextIndex = 0;

    async function worker() {
      while (nextIndex < items.length) {
        const index = nextIndex;
        nextIndex += 1;
        results[index] = await mapper(items[index], index);
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(concurrency, items.length) }, () => worker()),
    );
    return results;
  }

  async function fetchJson(url) {
    const response = await fetch(url, {
      cache: "no-store",
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!response.ok) throw createResponseError(response, "GitHub");
    return response.json();
  }

  async function fetchText(url, cache) {
    const response = await fetch(url, { cache });
    if (!response.ok) throw createResponseError(response, url);
    return response.text();
  }

  function createResponseError(response, resource) {
    if (response.status === 403 && response.headers.get("x-ratelimit-remaining") === "0") {
      return new Error("GitHub's public request limit has been reached. Please wait and refresh.");
    }
    return new Error(`${resource} returned ${response.status} ${response.statusText}.`);
  }

  function populateFilters() {
    populateVocabularySelect(
      ui.documentType,
      uniqueRecordValues("document_type"),
      state.vocabularies["document-types"],
    );
    populateVocabularySelect(
      ui.publicationStage,
      uniqueRecordValues("publication_stage"),
      state.vocabularies["publication-stages"],
    );
    populateVocabularySelect(
      ui.status,
      uniqueRecordValues("pip_litdb_status", true),
      state.vocabularies["record-statuses"],
      "No status",
    );

    const years = uniqueRecordValues("publication_year")
      .map(Number)
      .filter(Number.isFinite);
    populateYearRange(years);
  }

  function populateYearRange(years) {
    if (years.length === 0) {
      state.yearBounds = { min: null, max: null };
      [ui.yearMinInput, ui.yearMaxInput].forEach((input) => {
        input.value = "";
        input.removeAttribute("min");
        input.removeAttribute("max");
      });
      [ui.yearMinSlider, ui.yearMaxSlider].forEach((slider) => {
        slider.min = "0";
        slider.max = "0";
        slider.value = "0";
      });
      updateYearRangeTrack();
      return;
    }

    const min = Math.min(...years);
    const max = Math.max(...years);
    state.yearBounds = { min, max };
    [ui.yearMinInput, ui.yearMaxInput, ui.yearMinSlider, ui.yearMaxSlider].forEach((control) => {
      control.min = String(min);
      control.max = String(max);
    });
    setYearRangeValues(min, max);
  }

  function syncYearRangeFromNumber(edge, force) {
    const input = edge === "min" ? ui.yearMinInput : ui.yearMaxInput;
    const { min: lowerBound, max: upperBound } = state.yearBounds;
    if (lowerBound === null || upperBound === null) return;

    const parsed = Number(input.value);
    const valueCanCommit = input.value !== "" && Number.isInteger(parsed);
    const valueIsValid = valueCanCommit && input.checkValidity();
    const completeYearLength = String(lowerBound).length;
    if (!force && !valueIsValid && (!valueCanCommit || input.value.length < completeYearLength)) {
      return;
    }

    const fallback = edge === "min" ? lowerBound : upperBound;
    const value = clampYear(valueCanCommit ? parsed : fallback, lowerBound, upperBound);
    let min = Number(ui.yearMinSlider.value);
    let max = Number(ui.yearMaxSlider.value);

    if (edge === "min") {
      min = value;
      if (min > max) max = min;
    } else {
      max = value;
      if (max < min) min = max;
    }

    setYearRangeValues(min, max);
    renderDatabase();
  }

  function syncYearRangeFromSlider(edge, shouldRender) {
    let min = Number(ui.yearMinSlider.value);
    let max = Number(ui.yearMaxSlider.value);

    if (edge === "min" && min > max) min = max;
    if (edge === "max" && max < min) max = min;

    setYearRangeValues(min, max);
    if (shouldRender) renderDatabase();
  }

  function setYearRangeValues(min, max) {
    const { min: lowerBound, max: upperBound } = state.yearBounds;
    if (lowerBound === null || upperBound === null) return;

    const normalizedMin = clampYear(Math.round(min), lowerBound, upperBound);
    const normalizedMax = clampYear(Math.round(max), lowerBound, upperBound);
    const orderedMin = Math.min(normalizedMin, normalizedMax);
    const orderedMax = Math.max(normalizedMin, normalizedMax);

    ui.yearMinInput.value = String(orderedMin);
    ui.yearMaxInput.value = String(orderedMax);
    ui.yearMinSlider.value = String(orderedMin);
    ui.yearMaxSlider.value = String(orderedMax);
    updateYearRangeTrack();
  }

  function updateYearRangeTrack() {
    const { min: lowerBound, max: upperBound } = state.yearBounds;
    if (lowerBound === null || upperBound === null || lowerBound === upperBound) {
      ui.yearSlider.style.setProperty("--year-range-start", "0%");
      ui.yearSlider.style.setProperty("--year-range-end", "100%");
      return;
    }

    const span = upperBound - lowerBound;
    const start = ((Number(ui.yearMinSlider.value) - lowerBound) / span) * 100;
    const end = ((Number(ui.yearMaxSlider.value) - lowerBound) / span) * 100;
    ui.yearSlider.style.setProperty("--year-range-start", `${start}%`);
    ui.yearSlider.style.setProperty("--year-range-end", `${end}%`);
  }

  function bringYearSliderThumbToFront(activeSlider) {
    ui.yearMinSlider.style.zIndex = activeSlider === ui.yearMinSlider ? "4" : "3";
    ui.yearMaxSlider.style.zIndex = activeSlider === ui.yearMaxSlider ? "4" : "3";
  }

  function clampYear(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function getSelectedYearRange() {
    if (state.yearBounds.min === null || state.yearBounds.max === null) return null;
    return {
      min: Number(ui.yearMinSlider.value),
      max: Number(ui.yearMaxSlider.value),
    };
  }

  function isYearRangeActive() {
    const selected = getSelectedYearRange();
    return Boolean(
      selected &&
        (selected.min > state.yearBounds.min || selected.max < state.yearBounds.max),
    );
  }

  function populateVocabularySelect(select, values, vocabulary, emptyLabel) {
    removeGeneratedOptions(select);
    values
      .sort((a, b) => labelFor(vocabulary, a, emptyLabel).localeCompare(labelFor(vocabulary, b, emptyLabel)))
      .forEach((value) => addOption(select, value, labelFor(vocabulary, value, emptyLabel)));
  }

  function uniqueRecordValues(field, includeMissing = false) {
    const values = new Set();
    state.records.forEach((record) => {
      if (record[field] !== undefined && record[field] !== null && record[field] !== "") {
        values.add(String(record[field]));
      } else if (includeMissing) {
        values.add("__missing__");
      }
    });
    return [...values];
  }

  function removeGeneratedOptions(select) {
    [...select.options].slice(1).forEach((option) => option.remove());
  }

  function addOption(select, value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }

  function labelFor(vocabulary, value, emptyLabel = "Not specified") {
    if (value === "__missing__") return emptyLabel;
    return vocabulary?.[value]?.label || humanize(value);
  }

  function renderDatabase() {
    if (state.isLoading) return;

    const visibleRecords = getVisibleRecords();
    const total = state.records.length;
    const databaseIsEmpty = total === 0;
    ui.records.replaceChildren(...visibleRecords.map(createRecordCard));
    ui.records.setAttribute("aria-busy", "false");
    ui.empty.hidden = visibleRecords.length !== 0;
    ui.records.hidden = visibleRecords.length === 0;
    ui.emptyTitle.textContent = databaseIsEmpty ? "No records yet" : "No matching records";
    ui.emptyDetail.textContent = databaseIsEmpty
      ? "The public database is ready for its first YAML record. Refresh after a record is added."
      : "Try a broader search or clear the active filters.";
    ui.emptyClearFilters.hidden = databaseIsEmpty;

    ui.recordCount.textContent =
      visibleRecords.length === total
        ? `${formatNumber(total)} ${pluralize(total, "record")}`
        : `${formatNumber(visibleRecords.length)} of ${formatNumber(total)} records`;

    const hasActiveFilters = Boolean(
      ui.search.value ||
        ui.documentType.value ||
        ui.publicationStage.value ||
        isYearRangeActive() ||
        ui.status.value,
    );
    ui.clearFilters.disabled = !hasActiveFilters;
  }

  function getVisibleRecords() {
    const query = normalize(ui.search.value);
    const yearRange = getSelectedYearRange();
    const filterByYear = isYearRangeActive();
    const records = state.records.filter((record) => {
      if (ui.documentType.value && record.document_type !== ui.documentType.value) return false;
      if (ui.publicationStage.value && record.publication_stage !== ui.publicationStage.value) return false;
      if (filterByYear) {
        const year = Number(record.publication_year);
        if (!Number.isFinite(year) || year < yearRange.min || year > yearRange.max) return false;
      }
      if (
        ui.status.value &&
        (ui.status.value === "__missing__"
          ? Boolean(record.pip_litdb_status)
          : record.pip_litdb_status !== ui.status.value)
      ) {
        return false;
      }
      if (!query) return true;
      return searchableRecordText(record).includes(query);
    });

    return records.sort((a, b) => {
      switch (ui.sort.value) {
        case "year-asc":
          return compareYears(a, b, 1) || a.pip_litdb_id.localeCompare(b.pip_litdb_id);
        case "title-asc":
          return String(a.title || "").localeCompare(String(b.title || ""));
        case "id-asc":
          return a.pip_litdb_id.localeCompare(b.pip_litdb_id);
        case "year-desc":
        default:
          return compareYears(a, b, -1) || a.pip_litdb_id.localeCompare(b.pip_litdb_id);
      }
    });
  }

  function compareYears(a, b, direction) {
    const yearA = Number(a.publication_year) || 0;
    const yearB = Number(b.publication_year) || 0;
    return (yearA - yearB) * direction;
  }

  function searchableRecordText(record) {
    return normalize(
      [
        record.pip_litdb_id,
        record.title,
        record.journal,
        record.doi,
        record.url,
        record.publication_year,
        record.document_type,
        record.publication_stage,
        record.pip_litdb_status,
        record.pip_litdb_notes,
        ...(Array.isArray(record.authors) ? record.authors.map((author) => author?.name) : []),
      ]
        .filter(Boolean)
        .join(" "),
    );
  }

  function createRecordCard(record) {
    const titleId = `record-title-${record.pip_litdb_id}`;
    const openDetails = () => openRecord(record.pip_litdb_id);
    const card = element("article", {
      className: "record-card",
      attributes: {
        "aria-labelledby": titleId,
      },
    });
    card.addEventListener("click", (event) => {
      if (event.target.closest("a, button, input, select, textarea, summary, [role='button'], [role='link']")) {
        return;
      }
      openDetails();
    });

    const title = element("h3", {
      id: titleId,
      text: record.title || "Untitled record",
    });
    const authors = element("p", {
      className: "authors",
      text: formatAuthors(record.authors),
    });
    const journal = element("span", {
      className: "journal",
      text: record.journal || "Journal not specified",
    });
    const journalRow = element("p", { className: "journal-row" }, [
      element("span", {
        className: "record-year-inline",
        text: record.publication_year ? String(record.publication_year) : "Year unknown",
      }),
      element("span", {
        className: "journal-separator",
        text: "·",
        attributes: { "aria-hidden": "true" },
      }),
      journal,
    ]);
    const doiUrl = safeHttpUrl(record.doi ? `https://doi.org/${record.doi}` : null);
    if (doiUrl) {
      journalRow.append(
        element("span", {
          className: "journal-separator",
          text: "·",
          attributes: { "aria-hidden": "true" },
        }),
        element("a", {
          className: "record-doi-link",
          text: record.doi,
          attributes: {
            href: doiUrl,
            target: "_blank",
            rel: "noreferrer",
            title: record.doi,
            "aria-label": `Open DOI ${record.doi}`,
          },
        }),
      );
    }

    const tags = element("div", { className: "tag-row" });
    tags.append(
      element("span", {
        className: "tag record-id-tag",
        text: `Paper ID ${record.pip_litdb_id}`,
      }),
    );
    if (record.document_type) {
      tags.append(
        element("span", {
          className: "tag",
          text: labelFor(state.vocabularies["document-types"], record.document_type),
        }),
      );
    }
    if (record.publication_stage) {
      tags.append(
        element("span", {
          className: "tag is-warm",
          text: labelFor(state.vocabularies["publication-stages"], record.publication_stage),
        }),
      );
    }

    const relationshipCount = Array.isArray(record.related_papers) ? record.related_papers.length : 0;
    const cardActions = element("div", { className: "record-card-actions" });
    if (relationshipCount > 0) {
      cardActions.append(
        element("span", {
          className: "relationship-count",
          text: `${relationshipCount} related ${pluralize(relationshipCount, "record")}`,
        }),
      );
    }
    const detailsButton = element("button", {
      className: "record-details-button",
      text: "View details →",
      attributes: {
        type: "button",
        "aria-label": `View details for ${record.title || `record ${record.pip_litdb_id}`}`,
      },
    });
    detailsButton.addEventListener("click", openDetails);
    cardActions.append(detailsButton);

    const bottom = element("div", { className: "record-card-bottom" }, [
      tags,
      cardActions,
    ]);

    card.append(title, authors, journalRow, bottom);
    return card;
  }

  function openRecord(id, updateHash = true) {
    const record = state.recordById.get(id);
    if (!record) return;

    ui.dialogId.textContent = `Paper ID ${record.pip_litdb_id}`;
    ui.dialogContent.replaceChildren(createRecordDetails(record));
    if (!ui.dialog.open) ui.dialog.showModal();

    if (updateHash && window.location.hash !== `#record-${id}`) {
      history.pushState(null, "", `#record-${id}`);
    }
  }

  function createRecordDetails(record) {
    const fragment = document.createDocumentFragment();
    const vocabulary = state.vocabularies;

    const tags = element("div", { className: "tag-row" });
    if (record.document_type) {
      tags.append(
        element("span", {
          className: "tag",
          text: labelFor(vocabulary["document-types"], record.document_type),
        }),
      );
    }
    if (record.publication_stage) {
      tags.append(
        element("span", {
          className: "tag is-warm",
          text: labelFor(vocabulary["publication-stages"], record.publication_stage),
        }),
      );
    }

    fragment.append(
      tags,
      element("h2", { id: "dialog-title", text: record.title || "Untitled record" }),
      element("p", { className: "dialog-authors", text: formatAuthors(record.authors) }),
    );

    const overview = createMetadataSection("Publication details");
    const overviewGrid = element("dl", { className: "metadata-grid" });
    appendMetadata(overviewGrid, "Journal", record.journal || "Not specified");
    appendMetadata(
      overviewGrid,
      "Publication year",
      record.publication_year ? String(record.publication_year) : "Not specified",
    );
    appendMetadata(
      overviewGrid,
      "Document type",
      labelFor(vocabulary["document-types"], record.document_type),
    );
    appendMetadata(
      overviewGrid,
      "Publication stage",
      labelFor(vocabulary["publication-stages"], record.publication_stage),
    );
    appendMetadata(
      overviewGrid,
      "Record status",
      record.pip_litdb_status
        ? labelFor(vocabulary["record-statuses"], record.pip_litdb_status)
        : "Not specified",
    );
    appendLinkMetadata(overviewGrid, "DOI", record.doi, record.doi ? `https://doi.org/${record.doi}` : null);
    appendLinkMetadata(overviewGrid, "Source URL", record.url ? "Open source page" : null, record.url);
    overview.append(overviewGrid);
    fragment.append(overview);

    if (Array.isArray(record.authors) && record.authors.length > 0) {
      const authorsSection = createMetadataSection(
        `${record.authors.length} ${pluralize(record.authors.length, "author")}`,
      );
      const authorList = element("ol", { className: "author-list" });
      record.authors.forEach((author) => {
        authorList.append(element("li", { text: author?.name || "Unnamed author" }));
      });
      authorsSection.append(authorList);
      fragment.append(authorsSection);
    }

    if (Array.isArray(record.related_papers) && record.related_papers.length > 0) {
      const relationships = createMetadataSection("Related records");
      const list = element("ul", { className: "relationship-list" });
      record.related_papers.forEach((relationship) => {
        const item = element("li");
        const relatedRecord = state.recordById.get(relationship.pip_litdb_id);
        const relationshipLabel = labelFor(
          vocabulary["relationship-types"],
          relationship.relationship_type,
        );

        if (relatedRecord) {
          const button = element("button", {
            className: "relationship-button",
            attributes: { type: "button" },
          });
          button.append(
            element("span", {}, [
              element("strong", {
                text: `${relationshipLabel} · Paper ID ${relationship.pip_litdb_id}`,
              }),
              element("span", { className: "relationship-title", text: relatedRecord.title }),
            ]),
            element("span", { className: "relationship-arrow", text: "→" }),
          );
          button.addEventListener("click", () => openRecord(relationship.pip_litdb_id));
          item.append(button);
        } else {
          item.append(
            element("div", { className: "relationship-missing" }, [
              element("span", {
                text: `${relationshipLabel} · Paper ID ${relationship.pip_litdb_id}`,
              }),
              element("span", { className: "relationship-title", text: "Record not loaded" }),
            ]),
          );
        }
        list.append(item);
      });
      relationships.append(list);
      fragment.append(relationships);
    }

    if (record.pip_litdb_notes) {
      const notes = createMetadataSection("PIP LitDB notes");
      notes.append(element("p", { className: "notes", text: record.pip_litdb_notes }));
      fragment.append(notes);
    }

    const additionalFields = Object.entries(record).filter(
      ([key]) => key !== "pip_litdb_id" && !KNOWN_RECORD_FIELDS.has(key),
    );
    if (additionalFields.length > 0) {
      const additional = createMetadataSection("Additional metadata");
      const grid = element("dl", { className: "metadata-grid" });
      additionalFields.forEach(([key, value]) => {
        const wrapper = element("div", { className: "metadata-item" });
        wrapper.append(
          element("dt", { text: humanize(key) }),
          element("dd", {}, [
            element("pre", {
              className: "additional-value",
              text: typeof value === "string" ? value : JSON.stringify(value, null, 2),
            }),
          ]),
        );
        grid.append(wrapper);
      });
      additional.append(grid);
      fragment.append(additional);
    }

    return fragment;
  }

  function createMetadataSection(title) {
    const section = element("section", { className: "metadata-section" });
    section.append(element("h3", { text: title }));
    return section;
  }

  function appendMetadata(list, label, value) {
    const wrapper = element("div", { className: "metadata-item" });
    wrapper.append(element("dt", { text: label }), element("dd", { text: value }));
    list.append(wrapper);
  }

  function appendLinkMetadata(list, label, text, url) {
    const wrapper = element("div", { className: "metadata-item" });
    const value = element("dd");
    const safeUrl = safeHttpUrl(url);
    if (text && safeUrl) {
      value.append(
        element("a", {
          text,
          attributes: { href: safeUrl, target: "_blank", rel: "noreferrer" },
        }),
      );
    } else {
      value.textContent = text || "Not specified";
    }
    wrapper.append(element("dt", { text: label }), value);
    list.append(wrapper);
  }

  function safeHttpUrl(value) {
    if (!value) return null;
    try {
      const url = new URL(value, document.baseURI);
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  }

  function openRecordFromHash() {
    const match = window.location.hash.match(/^#record-(\d{5})$/);
    if (match && state.recordById.has(match[1])) {
      openRecord(match[1], false);
      return;
    }
    if (ui.dialog.open) ui.dialog.close();
  }

  function clearRecordHash() {
    if (/^#record-\d{5}$/.test(window.location.hash)) {
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
  }

  function closeDialogFromBackdrop(event) {
    if (event.target !== ui.dialog) return;
    const bounds = ui.dialog.getBoundingClientRect();
    const isInside =
      event.clientX >= bounds.left &&
      event.clientX <= bounds.right &&
      event.clientY >= bounds.top &&
      event.clientY <= bounds.bottom;
    if (!isInside) ui.dialog.close();
  }

  function resetFilters() {
    ui.search.value = "";
    ui.documentType.value = "";
    ui.publicationStage.value = "";
    if (state.yearBounds.min !== null && state.yearBounds.max !== null) {
      setYearRangeValues(state.yearBounds.min, state.yearBounds.max);
    }
    ui.status.value = "";
    ui.sort.value = "year-desc";
    renderDatabase();
    ui.search.focus();
  }

  function setLoadingState() {
    state.isLoading = true;
    state.records = [];
    state.recordById = new Map();
    state.yearBounds = { min: null, max: null };
    setControlsDisabled(true);
    ui.records.replaceChildren();
    ui.records.hidden = false;
    ui.records.setAttribute("aria-busy", "true");
    ui.loading.hidden = false;
    ui.empty.hidden = true;
    ui.error.hidden = true;
    ui.recordCount.textContent = "Loading records…";
    ui.sourceName.textContent = "Connecting…";
    ui.sourceLink.hidden = true;
    ui.sourceDot.className = "source-dot";
  }

  function setSourceReady(source) {
    ui.loading.hidden = true;
    ui.error.hidden = true;
    ui.sourceDot.className = "source-dot is-ready";
    ui.sourceName.textContent = source.name;
    ui.sourceLink.textContent = source.detail;
    ui.sourceLink.href = source.url;
    ui.sourceLink.hidden = false;
  }

  function setErrorState(error) {
    ui.loading.hidden = true;
    ui.records.hidden = true;
    ui.empty.hidden = true;
    ui.error.hidden = false;
    ui.recordCount.textContent = "Database unavailable";
    ui.sourceDot.className = "source-dot is-error";
    ui.sourceName.textContent = "Connection failed";
    ui.sourceLink.hidden = true;
    ui.errorTitle.textContent = "The records could not be loaded.";
    ui.errorDetail.textContent = error.message || "An unexpected error occurred.";
  }

  function setControlsDisabled(disabled) {
    [ui.search, ui.documentType, ui.publicationStage, ui.status, ui.sort].forEach(
      (control) => {
        control.disabled = disabled;
      },
    );
    ui.yearRange.disabled = disabled || state.yearBounds.min === null;
    ui.clearFilters.disabled = disabled;
  }

  function formatAuthors(authors) {
    if (!Array.isArray(authors) || authors.length === 0) return "Authors not specified";
    return authors.map((author) => author?.name).filter(Boolean).join(", ");
  }

  function humanize(value) {
    if (!value) return "Not specified";
    return String(value)
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function normalize(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLocaleLowerCase();
  }

  function formatNumber(value) {
    return new Intl.NumberFormat().format(value);
  }

  function pluralize(count, singular) {
    return count === 1 ? singular : `${singular}s`;
  }

  function trimSlashes(value) {
    return String(value).replace(/^\/+|\/+$/g, "");
  }

  function ensureTrailingSlash(value) {
    return value.endsWith("/") ? value : `${value}/`;
  }

  function element(tagName, options = {}, children = []) {
    const node = document.createElement(tagName);
    if (options.id) node.id = options.id;
    if (options.className) node.className = options.className;
    if (options.text !== undefined && options.text !== null) node.textContent = String(options.text);
    if (options.attributes) {
      Object.entries(options.attributes).forEach(([name, value]) => node.setAttribute(name, value));
    }
    children.forEach((child) => node.append(child));
    return node;
  }
})();
