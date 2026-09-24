// Source Configuration Manager
class SourceConfigManager {
    constructor() {
        this.configurationVersion = 4;
        this.sources = {
            reddit: {
                enabled: false,
                subreddits: ['SQLServer', 'Database', 'MicrosoftFabric'],
                sort: 'new',
                timeFilter: 'month',
                postTypes: ['all'],
                maxItems: 100
            },
            github: {
                enabled: false,
                repositories: [
                    {
                        owner: 'dotnet',
                        repo: 'SqlClient',
                        enabled: true
                    }
                ],
                state: 'all',
                labels: [],
                maxItems: 100
            },
            githubIssues: {
                enabled: true,
                repositories: [
                    {
                        owner: 'microsoft',
                        repo: 'vscode-mssql',
                        enabled: true
                    },
                    {
                        owner: 'microsoft',
                        repo: 'sqltoolsservice',
                        enabled: true
                    },
                    {
                        owner: 'microsoft',
                        repo: 'DacFx',
                        enabled: true
                    },
                    {
                        owner: 'microsoft',
                        repo: 'go-sqlcmd',
                        enabled: true
                    },
                    {
                        owner: 'dotnet',
                        repo: 'SqlClient',
                        enabled: true
                    },
                    {
                        owner: 'microsoft',
                        repo: 'mssql-docker',
                        enabled: true
                    }
                ],
                state: 'all',
                labels: [],
                maxItems: 100
            },
            stackoverflow: {
                enabled: true,
                tags: ['sql-server', 'azure-sql-database', 'azure-sql-managed-instance'],
                maxItems: 100
            },
            dbaStackExchange: {
                enabled: true,
                tags: ['sql-server', 'azure-sql-database'],
                maxItems: 100
            },
            microsoftQA: {
                enabled: true,
                maxItems: 100
            },
            techCommunity: {
                enabled: true,
                feeds: [
                    'https://cloudblogs.microsoft.com/sqlserver/feed/',
                    'https://devblogs.microsoft.com/azure-sql/feed/'
                ],
                maxItems: 100
            },
            hackerNews: {
                enabled: true,
                queries: ['SQL Server', 'Azure SQL Database', 'Azure SQL Managed Instance'],
                days: 180,
                maxItems: 100
            },
            devCommunity: {
                enabled: true,
                tags: ['sqlserver', 'azuresql', 'mssql'],
                maxItems: 100
            },
            fabricCommunity: {
                enabled: false,
                maxItems: 100
            },
            ado: {
                enabled: false,
                parentWorkItem: '',
                workItemTypes: ['Bug', 'Feature', 'User Story'],
                states: ['New', 'Active', 'Resolved'],
                maxItems: 100
            }
        };
        
        this.settings = {
            topic: 'SQL Server and Azure SQL',
            timeRangeMonths: 12,
            maxItemsPerSource: 100,
            candidateMultiplier: 10,
            includeReplies: true,
            autoSelectPublicSources: true,
            respectRateLimits: true,
            keywords: [],
            duplicateDetection: true,
            languageFilter: 'all',
            sentimentThreshold: 0
        };
        
        this.init();
    }
    
    init() {
        this.loadConfiguration();
        this.setupEventListeners();
        this.renderSourceCards();
        this.renderSettings();
        this.renderQuickSetup();
        this.updateActiveSourcesCount();
    }
    
    loadConfiguration() {
        const savedConfig = localStorage.getItem('feedbackCollectorConfig');
        if (savedConfig) {
            try {
                const config = JSON.parse(savedConfig);
                const savedVersion = Number(config.version || 1);
                const storedSources = config.sources && typeof config.sources === 'object'
                    ? config.sources
                    : {};
                const sources = savedVersion < this.configurationVersion
                    ? this.migrateLegacySources(storedSources, savedVersion)
                    : storedSources;

                Object.entries(sources).forEach(([sourceId, source]) => {
                    if (
                        this.sources[sourceId]
                        && source
                        && typeof source === 'object'
                        && !Array.isArray(source)
                    ) {
                        this.sources[sourceId] = {
                            ...this.sources[sourceId],
                            ...source
                        };
                    }
                });
                if (config.settings && typeof config.settings === 'object') {
                    this.settings = { ...this.settings, ...config.settings };
                }
                if (savedVersion < 4) {
                    if (!String(this.settings.topic || '').trim()) {
                        this.settings.topic = 'SQL Server and Azure SQL';
                    }
                    if (Number(this.settings.maxItemsPerSource) === 5) {
                        this.settings.maxItemsPerSource = 100;
                        Object.values(this.sources).forEach((source) => {
                            if (Number(source.maxItems) === 5) {
                                source.maxItems = 100;
                            }
                        });
                    }
                    this.settings.candidateMultiplier = 10;
                    this.settings.includeReplies = true;
                    this.settings.autoSelectPublicSources = true;
                }
                const redditSource = this.sources.reddit;
                const redditValues = Object.prototype.hasOwnProperty.call(
                    redditSource,
                    'subreddits'
                )
                    ? redditSource.subreddits
                    : redditSource.subreddit;
                redditSource.subreddits = this.parseSubreddits(redditValues);
                delete redditSource.subreddit;
                if (!['relevance', 'hot', 'top', 'new', 'comments'].includes(redditSource.sort)) {
                    redditSource.sort = 'new';
                }
                
                // Ensure numeric settings are properly converted to integers
                if (this.settings.maxItemsPerSource) {
                    this.settings.maxItemsPerSource = parseInt(this.settings.maxItemsPerSource);
                }
                if (this.settings.timeRangeMonths) {
                    this.settings.timeRangeMonths = parseInt(this.settings.timeRangeMonths);
                }
                if (this.settings.candidateMultiplier) {
                    this.settings.candidateMultiplier = parseInt(this.settings.candidateMultiplier);
                }
                
                // Ensure source maxItems are integers
                Object.keys(this.sources).forEach(sourceId => {
                    if (this.sources[sourceId].maxItems) {
                        this.sources[sourceId].maxItems = parseInt(this.sources[sourceId].maxItems);
                    }
                });
                
                // Sync individual source maxItems with global setting if they haven't been customized
                // Only do this if the global setting exists and sources are using default values
                if (this.settings.maxItemsPerSource) {
                    Object.keys(this.sources).forEach(sourceId => {
                        // If source maxItems is still at old default (100) or matches global setting, update it
                        if (!this.sources[sourceId].maxItems || this.sources[sourceId].maxItems === 100) {
                            this.sources[sourceId].maxItems = this.settings.maxItemsPerSource;
                        }
                    });
                }

                if (savedVersion < this.configurationVersion) {
                    this.saveConfiguration();
                }
            } catch (e) {
                console.error('Error loading configuration:', e);
            }
        }
        
        // Load keywords from server
        this.loadKeywords();
    }

    migrateLegacySources(storedSources, savedVersion) {
        const sources = { ...storedSources };
        const legacyFabricRepos = new Set([
            'microsoft/microsoft-fabric-workload-development-sample',
            'microsoft/fabric-extensibility-toolkit',
            'microsoft/microsoft-fabric-tools-workload'
        ]);
        const isLegacyFabricRepoSet = (repositories) => (
            Array.isArray(repositories)
            && repositories.length === legacyFabricRepos.size
            && repositories.every((repo) => (
                repo
                && legacyFabricRepos.has(
                    `${String(repo.owner).toLowerCase()}/${String(repo.repo).toLowerCase()}`
                )
            ))
        );
        const redditSource = sources.reddit || {};
        const githubSource = sources.github || {};
        const githubIssuesSource = sources.githubIssues || {};
        const fabricSource = sources.fabricCommunity || {};
        const adoSource = sources.ado || {};

        const isLegacyRedditDefault = (
            redditSource.subreddit === 'MicrosoftFabric'
            || (
                savedVersion === 2
                && redditSource.subreddit === 'SQLServer'
                && redditSource.enabled === false
                && Number(redditSource.maxItems) === 5
            )
        );
        const migratedSubreddits = isLegacyRedditDefault
            ? [...this.sources.reddit.subreddits]
            : this.parseSubreddits(
                redditSource.subreddits || redditSource.subreddit
            );
        if (Object.keys(redditSource).length > 0) {
            sources.reddit = {
                ...redditSource,
                enabled: isLegacyRedditDefault ? false : redditSource.enabled,
                subreddits: migratedSubreddits
            };
            delete sources.reddit.subreddit;
        }
        if (isLegacyFabricRepoSet(githubSource.repositories)) {
            sources.github = {
                ...githubSource,
                enabled: false,
                repositories: this.sources.github.repositories.map((repo) => ({ ...repo }))
            };
        }
        if (isLegacyFabricRepoSet(githubIssuesSource.repositories)) {
            sources.githubIssues = {
                ...githubIssuesSource,
                repositories: this.sources.githubIssues.repositories.map((repo) => ({ ...repo }))
            };
        }
        if (
            fabricSource.enabled === true
            && Number(fabricSource.maxItems) === 5
        ) {
            sources.fabricCommunity = {
                ...fabricSource,
                enabled: false
            };
        }
        if (String(adoSource.parentWorkItem || '') === '1319103') {
            sources.ado = {
                ...adoSource,
                enabled: false,
                parentWorkItem: ''
            };
        }

        return sources;
    }

    parseSubreddits(value) {
        const values = Array.isArray(value) ? value : [value || ''];
        const names = [];
        const seen = new Set();

        values.forEach((rawValue) => {
            String(rawValue).split(/[,\r\n]+/).forEach((entry) => {
                const name = entry
                    .trim()
                    .replace(/^https?:\/\/(?:www\.)?reddit\.com\/r\//i, '')
                    .replace(/^\/?r\//i, '')
                    .replace(/\s+/g, '')
                    .replace(/^\/+|\/+$/g, '');
                const key = name.toLowerCase();
                if (name && !seen.has(key)) {
                    names.push(name);
                    seen.add(key);
                }
            });
        });

        return names;
    }
    
    async loadKeywords() {
        try {
            const response = await fetch('/api/keywords');
            const keywords = await response.json();
            this.settings.keywords = keywords;
            this.renderKeywordsInSettings();
        } catch (e) {
            console.error('Error loading keywords:', e);
        }
    }
    
    saveConfiguration() {
        const config = {
            version: this.configurationVersion,
            sources: this.sources,
            settings: this.settings
        };
        localStorage.setItem('feedbackCollectorConfig', JSON.stringify(config));
    }
    
    updateActiveSourcesCount() {
        // Count how many sources are enabled
        const enabledCount = Object.values(this.sources).filter(source => source.enabled).length;
        
        // Update the dashboard stat
        const statElement = document.getElementById('statActiveSources');
        if (statElement) {
            statElement.textContent = enabledCount;
        }
    }
    
    setupEventListeners() {
        // Source toggle listeners
        document.addEventListener('change', (e) => {
            if (e.target.matches('.source-toggle')) {
                this.handleSourceToggle(e.target);
            }
        });
        
        // Configuration button listeners
        document.addEventListener('click', (e) => {
            if (e.target.matches('.btn-configure') || e.target.closest('.btn-configure')) {
                this.toggleSourceConfig(e.target.closest('.source-card'));
            }
            
            // Add repository button
            if (e.target.matches('.btn-add-repo') || e.target.closest('.btn-add-repo')) {
                this.handleAddRepository(e.target.closest('.source-card'));
            }
            
            // Remove repository button
            if (e.target.matches('.btn-remove-repo') || e.target.closest('.btn-remove-repo')) {
                const button = e.target.closest('.btn-remove-repo');
                const repoIndex = parseInt(button.dataset.repoIndex);
                this.handleRemoveRepository(repoIndex);
            }
            
            // GitHub Discussions - Add repository button
            if (e.target.matches('.btn-add-github-repo') || e.target.closest('.btn-add-github-repo')) {
                this.handleAddGithubRepository(e.target.closest('.source-card'));
            }
            
            // GitHub Discussions - Remove repository button
            if (e.target.matches('.btn-remove-github-repo') || e.target.closest('.btn-remove-github-repo')) {
                const button = e.target.closest('.btn-remove-github-repo');
                const repoIndex = parseInt(button.dataset.repoIndex);
                this.handleRemoveGithubRepository(repoIndex);
            }
        });
        
        // Input change listeners
        document.addEventListener('input', (e) => {
            if (e.target.matches('.source-input')) {
                this.handleSourceInputChange(e.target);
            }
        });
        
        // Settings change listeners
        document.addEventListener('change', (e) => {
            if (e.target.matches('.setting-input')) {
                this.handleSettingChange(e.target);
            }
            
            // Repository toggle listeners
            if (e.target.matches('.repo-toggle')) {
                this.handleRepoToggle(e.target);
            }
            
            // GitHub repo toggle listeners
            if (e.target.matches('.github-repo-toggle')) {
                this.handleGithubRepoToggle(e.target);
            }
        });

        document.addEventListener('click', (e) => {
            if (e.target.matches('#previewSearchPlanBtn') || e.target.closest('#previewSearchPlanBtn')) {
                this.previewSearchPlan();
            }
            if (e.target.matches('#openAdvancedSettingsBtn') || e.target.closest('#openAdvancedSettingsBtn')) {
                const navItem = Array.from(document.querySelectorAll('.nav-item')).find(
                    (item) => String(item.getAttribute('onclick') || '').includes('view-sources')
                );
                if (typeof window.switchView === 'function') {
                    window.switchView('view-sources', navItem || null);
                }
            }
        });
    }
    
    handleAddRepository(sourceCard) {
        const ownerInput = sourceCard.querySelector('#newRepoOwner');
        const repoInput = sourceCard.querySelector('#newRepoName');
        
        const owner = ownerInput.value.trim();
        const repo = repoInput.value.trim();
        
        if (!owner || !repo) {
            alert('Please enter both owner and repository name');
            return;
        }
        
        // Check for duplicates
        const existingRepos = this.sources.githubIssues.repositories || [];
        const isDuplicate = existingRepos.some(r => 
            r.owner.toLowerCase() === owner.toLowerCase() && 
            r.repo.toLowerCase() === repo.toLowerCase()
        );
        
        if (isDuplicate) {
            alert('This repository is already in the list');
            return;
        }
        
        // Add the new repository
        if (!this.sources.githubIssues.repositories) {
            this.sources.githubIssues.repositories = [];
        }
        
        this.sources.githubIssues.repositories.push({
            owner: owner,
            repo: repo,
            enabled: true
        });
        
        // Clear inputs
        ownerInput.value = '';
        repoInput.value = '';
        
        // Re-render the source card
        this.renderSourceCards();
        this.saveConfiguration();
        
        // Re-open the configuration panel
        setTimeout(() => {
            const newCard = document.querySelector('[data-source="githubIssues"]');
            const configDiv = newCard.querySelector('.source-config');
            configDiv.classList.add('expanded');
        }, 100);
    }
    
    handleRemoveRepository(repoIndex) {
        if (!confirm('Are you sure you want to remove this repository?')) {
            return;
        }
        
        this.sources.githubIssues.repositories.splice(repoIndex, 1);
        this.renderSourceCards();
        this.saveConfiguration();
        
        // Re-open the configuration panel
        setTimeout(() => {
            const card = document.querySelector('[data-source="githubIssues"]');
            const configDiv = card.querySelector('.source-config');
            configDiv.classList.add('expanded');
        }, 100);
    }
    
    handleRepoToggle(toggle) {
        const repoIndex = parseInt(toggle.dataset.repoIndex);
        this.sources.githubIssues.repositories[repoIndex].enabled = toggle.checked;
        this.updateSourceStatus('githubIssues');
        this.saveConfiguration();
    }
    
    handleAddGithubRepository(sourceCard) {
        const ownerInput = sourceCard.querySelector('#newGithubRepoOwner');
        const repoInput = sourceCard.querySelector('#newGithubRepoName');
        
        const owner = ownerInput.value.trim();
        const repo = repoInput.value.trim();
        
        if (!owner || !repo) {
            alert('Please enter both owner and repository name');
            return;
        }
        
        // Check for duplicates
        const existingRepos = this.sources.github.repositories || [];
        const isDuplicate = existingRepos.some(r => 
            r.owner.toLowerCase() === owner.toLowerCase() && 
            r.repo.toLowerCase() === repo.toLowerCase()
        );
        
        if (isDuplicate) {
            alert('This repository is already in the list');
            return;
        }
        
        // Add the new repository
        if (!this.sources.github.repositories) {
            this.sources.github.repositories = [];
        }
        
        this.sources.github.repositories.push({
            owner: owner,
            repo: repo,
            enabled: true
        });
        
        // Clear inputs
        ownerInput.value = '';
        repoInput.value = '';
        
        // Re-render the source card
        this.renderSourceCards();
        this.saveConfiguration();
        
        // Re-open the configuration panel
        setTimeout(() => {
            const newCard = document.querySelector('[data-source="github"]');
            const configDiv = newCard.querySelector('.source-config');
            configDiv.classList.add('expanded');
        }, 100);
    }
    
    handleRemoveGithubRepository(repoIndex) {
        if (!confirm('Are you sure you want to remove this repository?')) {
            return;
        }
        
        this.sources.github.repositories.splice(repoIndex, 1);
        this.renderSourceCards();
        this.saveConfiguration();
        
        // Re-open the configuration panel
        setTimeout(() => {
            const card = document.querySelector('[data-source="github"]');
            const configDiv = card.querySelector('.source-config');
            configDiv.classList.add('expanded');
        }, 100);
    }
    
    handleGithubRepoToggle(toggle) {
        const repoIndex = parseInt(toggle.dataset.repoIndex);
        this.sources.github.repositories[repoIndex].enabled = toggle.checked;
        this.updateSourceStatus('github');
        this.saveConfiguration();
    }
    
    handleSourceToggle(toggle) {
        const sourceCard = toggle.closest('.source-card');
        const sourceId = sourceCard.dataset.source;
        this.sources[sourceId].enabled = toggle.checked;
        this.settings.autoSelectPublicSources = false;
        const quickAllPublicSources = document.getElementById('quickAllPublicSources');
        if (quickAllPublicSources) quickAllPublicSources.checked = false;
        
        // Update UI
        sourceCard.classList.toggle('disabled', !toggle.checked);
        this.updateSourceStatus(sourceId);
        this.saveConfiguration();
        this.updateActiveSourcesCount();
    }
    
    toggleSourceConfig(sourceCard) {
        const configDiv = sourceCard.querySelector('.source-config');
        const isExpanded = configDiv.classList.contains('expanded');
        
        // Close all other configs
        document.querySelectorAll('.source-config.expanded').forEach(config => {
            if (config !== configDiv) {
                config.classList.remove('expanded');
            }
        });
        
        // Toggle current config
        configDiv.classList.toggle('expanded', !isExpanded);
        
        // Update button icon
        const button = sourceCard.querySelector('.btn-configure i');
        button.className = isExpanded ? 'bi bi-gear' : 'bi bi-gear-fill';
    }
    
    handleSourceInputChange(input) {
        const sourceCard = input.closest('.source-card');
        const sourceId = sourceCard.dataset.source;
        const field = input.dataset.field;
        let value = input.type === 'checkbox' ? input.checked : input.value;
        if (input.type === 'number') {
            value = parseInt(input.value);
        } else if (input.multiple) {
            value = Array.from(input.selectedOptions, (option) => option.value);
        } else if (sourceId === 'reddit' && field === 'subreddits') {
            value = this.parseSubreddits(input.value);
        }
        
        // Update source configuration
        if (field.includes('.')) {
            const [parent, child] = field.split('.');
            if (!this.sources[sourceId][parent]) {
                this.sources[sourceId][parent] = {};
            }
            this.sources[sourceId][parent][child] = value;
        } else {
            this.sources[sourceId][field] = value;
        }
        
        this.updateSourceStatus(sourceId);
        this.saveConfiguration();
    }
    
    handleSettingChange(input) {
        const field = input.dataset.field;
        let value = input.type === 'checkbox' ? input.checked : input.value;
        
        // Convert numeric fields to integers
        if (
            field === 'maxItemsPerSource'
            || field === 'timeRangeMonths'
            || field === 'candidateMultiplier'
            || input.type === 'number'
        ) {
            value = parseInt(value);
        }
        
        this.settings[field] = value;
        
        // Special handling for maxItemsPerSource - apply to all sources
        if (field === 'maxItemsPerSource') {
            Object.keys(this.sources).forEach(sourceId => {
                this.sources[sourceId].maxItems = parseInt(value);
            });
            // Re-render source cards to show updated values
            this.renderSourceCards();
        }
        
        this.saveConfiguration();
    }

    renderQuickSetup() {
        const topicInput = document.getElementById('quickTopic');
        const timeRange = document.getElementById('quickTimeRange');
        const resultLimit = document.getElementById('quickResultLimit');
        const includeReplies = document.getElementById('quickIncludeReplies');
        const allPublicSources = document.getElementById('quickAllPublicSources');
        if (topicInput) topicInput.value = this.settings.topic || '';
        if (timeRange) timeRange.value = String(this.settings.timeRangeMonths);
        if (resultLimit) resultLimit.value = String(this.settings.maxItemsPerSource);
        if (includeReplies) includeReplies.checked = this.settings.includeReplies !== false;
        if (allPublicSources) {
            allPublicSources.checked = this.settings.autoSelectPublicSources !== false;
        }
    }

    readQuickSettingsFromDom() {
        const topicInput = document.getElementById('quickTopic');
        const timeRange = document.getElementById('quickTimeRange');
        const resultLimit = document.getElementById('quickResultLimit');
        const includeReplies = document.getElementById('quickIncludeReplies');
        const allPublicSources = document.getElementById('quickAllPublicSources');
        return {
            ...this.settings,
            topic: topicInput
                ? topicInput.value.trim()
                : this.settings.topic,
            timeRangeMonths: timeRange
                ? parseInt(timeRange.value, 10)
                : this.settings.timeRangeMonths,
            maxItemsPerSource: resultLimit
                ? parseInt(resultLimit.value, 10)
                : this.settings.maxItemsPerSource,
            includeReplies: includeReplies
                ? includeReplies.checked
                : this.settings.includeReplies,
            autoSelectPublicSources: allPublicSources
                ? allPublicSources.checked
                : this.settings.autoSelectPublicSources
        };
    }

    syncQuickSetupFromDom() {
        const quickSettings = this.readQuickSettingsFromDom();
        const limitChanged = (
            Number(quickSettings.maxItemsPerSource)
            !== Number(this.settings.maxItemsPerSource)
        );
        this.settings = quickSettings;
        if (limitChanged) {
            const limit = Number(quickSettings.maxItemsPerSource);
            Object.values(this.sources).forEach((source) => {
                source.maxItems = limit;
            });
        }
        if (this.settings.autoSelectPublicSources !== false) {
            [
                'stackoverflow',
                'dbaStackExchange',
                'microsoftQA',
                'techCommunity',
                'hackerNews',
                'devCommunity',
                'githubIssues'
            ].forEach((sourceId) => {
                if (this.sources[sourceId]) this.sources[sourceId].enabled = true;
            });
        }
        this.saveConfiguration();
        this.updateActiveSourcesCount();
        this.renderSourceCards();
        this.renderSettings();
    }

    async previewSearchPlan() {
        const previewSettings = this.readQuickSettingsFromDom();
        const preview = document.getElementById('searchPlanPreview');
        if (!preview) return;
        if (!previewSettings.topic) {
            preview.textContent = 'Describe the feedback you want to find first.';
            preview.className = 'fluent-alert fluent-alert-warning mt-3';
            return;
        }
        preview.textContent = 'Preparing source searches...';
        preview.className = 'fluent-alert fluent-alert-info mt-3';
        try {
            const response = await fetch('/api/search-plan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(previewSettings)
            });
            const result = await response.json();
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.message || `HTTP ${response.status}`);
            }
            const terms = Array.isArray(result.search_terms)
                ? result.search_terms
                : [];
            preview.replaceChildren();
            const heading = document.createElement('strong');
            heading.textContent = `${terms.length} source queries prepared`;
            preview.appendChild(heading);
            const copy = document.createElement('div');
            copy.className = 'small mt-1';
            copy.textContent = terms.join(' · ');
            preview.appendChild(copy);
            preview.className = 'fluent-alert fluent-alert-success mt-3';
        } catch (error) {
            preview.textContent = `Unable to prepare the search: ${error.message}`;
            preview.className = 'fluent-alert fluent-alert-error mt-3';
        }
    }
    
    updateSourceStatus(sourceId) {
        const sourceCard = document.querySelector(`[data-source="${sourceId}"]`);
        const statusElement = sourceCard.querySelector('.source-status');
        
        if (!this.sources[sourceId].enabled) {
            statusElement.textContent = 'Disabled';
            statusElement.className = 'source-status disabled';
            return;
        }
        
        // Check configuration validity
        let isValid = true;
        let message = 'Ready';
        
        switch (sourceId) {
            case 'reddit':
                const subreddits = this.sources.reddit.subreddits || [];
                if (subreddits.length === 0) {
                    isValid = false;
                    message = 'Add a subreddit';
                } else if (
                    subreddits.some(
                        (subreddit) => !/^[A-Za-z0-9_]{2,30}$/.test(subreddit)
                    )
                ) {
                    isValid = false;
                    message = 'Invalid subreddit';
                } else {
                    message = `${subreddits.length} subreddit${subreddits.length === 1 ? '' : 's'}`;
                }
                break;
            case 'github':
                const githubRepos = this.sources.github.repositories || [];
                const enabledGithubRepos = githubRepos.filter(r => r.enabled);
                if (enabledGithubRepos.length === 0) {
                    isValid = false;
                    message = 'No repositories enabled';
                }
                break;
            case 'githubIssues':
                const repos = this.sources.githubIssues.repositories || [];
                const enabledRepos = repos.filter(r => r.enabled);
                if (enabledRepos.length === 0) {
                    isValid = false;
                    message = 'No repositories enabled';
                }
                break;
            case 'ado':
                if (!this.sources.ado.parentWorkItem) {
                    isValid = false;
                    message = 'Missing work item';
                }
                break;
        }
        
        statusElement.textContent = message;
        statusElement.className = `source-status ${isValid ? 'ready' : 'error'}`;
    }
    
    renderSourceCards() {
        const container = document.getElementById('dataSources');
        if (!container) return;
        
        container.innerHTML = '';
        
        const sourceConfigs = [
            {
                id: 'stackoverflow',
                name: 'Stack Overflow',
                icon: 'bi bi-stack-overflow',
                description: 'SQL Server and Azure SQL questions'
            },
            {
                id: 'dbaStackExchange',
                name: 'DBA Stack Exchange',
                icon: 'bi bi-database',
                description: 'Database administrator questions and issues'
            },
            {
                id: 'microsoftQA',
                name: 'Microsoft Q&A',
                icon: 'bi bi-question-circle',
                description: 'Microsoft SQL Server and Azure SQL questions'
            },
            {
                id: 'techCommunity',
                name: 'Microsoft SQL Blogs',
                icon: 'bi bi-rss',
                description: 'Official SQL Server and Azure SQL posts and comments'
            },
            {
                id: 'hackerNews',
                name: 'Hacker News',
                icon: 'bi bi-newspaper',
                description: 'Recent public SQL Server and Azure SQL discussions'
            },
            {
                id: 'devCommunity',
                name: 'DEV Community',
                icon: 'bi bi-code-square',
                description: 'Public Forem posts tagged for SQL Server and Azure SQL'
            },
            {
                id: 'githubIssues',
                name: 'GitHub Issues',
                icon: 'bi bi-github',
                description: 'Issues from SQL Server and Azure SQL tools'
            },
            {
                id: 'reddit',
                name: 'Reddit',
                icon: 'bi bi-reddit',
                description: 'Optional SQL community discussions (credentials required)'
            },
            {
                id: 'github',
                name: 'GitHub Discussions',
                icon: 'bi bi-chat-dots',
                description: 'Optional repository discussions (GitHub token required)'
            },
            {
                id: 'fabricCommunity',
                name: 'Fabric Community Forums',
                icon: 'bi bi-people',
                description: 'Optional Microsoft Fabric community feedback'
            },
            {
                id: 'ado',
                name: 'Azure DevOps',
                icon: 'bi bi-kanban',
                description: 'Optional internal work items (credentials required)'
            }
        ];
        
        sourceConfigs.forEach(config => {
            const card = this.createSourceCard(config);
            container.appendChild(card);
        });
    }
    
    createSourceCard(config) {
        const source = this.sources[config.id];
        const card = document.createElement('div');
        card.className = 'source-card fluent-card';
        card.dataset.source = config.id;
        
        card.innerHTML = `
            <div class="source-header">
                <label class="fluent-toggle">
                    <input type="checkbox" class="source-toggle" ${source.enabled ? 'checked' : ''}>
                    <span class="fluent-toggle-slider"></span>
                </label>
                <div class="source-title">
                    <i class="${config.icon}"></i>
                    <div class="source-title-copy">
                        <span>${config.name}</span>
                        <small>${config.description}</small>
                    </div>
                </div>
                <span class="source-status ready">Ready</span>
                <button class="fluent-button-icon btn-configure" aria-label="Configure ${config.name}">
                    <i class="bi bi-gear"></i>
                </button>
            </div>
            <div class="source-config" id="${config.id}-config">
                ${this.renderSourceConfig(config.id)}
            </div>
        `;
        
        // Update initial status
        setTimeout(() => this.updateSourceStatus(config.id), 0);
        
        return card;
    }
    
    renderSourceConfig(sourceId) {
        const source = this.sources[sourceId];
        
        switch (sourceId) {
            case 'reddit':
                return `
                    <div class="config-field">
                        <label class="fluent-label">Subreddits:</label>
                        <textarea class="fluent-input source-input"
                                  data-field="subreddits" rows="3"
                                  placeholder="SQLServer, Database, MicrosoftFabric">${window.SafeDOM.escapeHtml((source.subreddits || []).join(', '))}</textarea>
                        <div class="source-info">
                            <span>Separate names with commas or new lines. Spaces and r/ prefixes are removed.</span>
                        </div>
                    </div>
                    <div class="config-field-row">
                        <div class="config-field">
                            <label class="fluent-label">Sort by:</label>
                            <select class="fluent-select source-input" data-field="sort">
                                <option value="relevance" ${source.sort === 'relevance' ? 'selected' : ''}>Relevance</option>
                                <option value="new" ${source.sort === 'new' ? 'selected' : ''}>New</option>
                                <option value="hot" ${source.sort === 'hot' ? 'selected' : ''}>Hot</option>
                                <option value="top" ${source.sort === 'top' ? 'selected' : ''}>Top</option>
                                <option value="comments" ${source.sort === 'comments' ? 'selected' : ''}>Most Comments</option>
                            </select>
                        </div>
                        <div class="config-field">
                            <label class="fluent-label">Time Filter:</label>
                            <select class="fluent-select source-input" data-field="timeFilter">
                                <option value="hour" ${source.timeFilter === 'hour' ? 'selected' : ''}>Past Hour</option>
                                <option value="day" ${source.timeFilter === 'day' ? 'selected' : ''}>Past Day</option>
                                <option value="week" ${source.timeFilter === 'week' ? 'selected' : ''}>Past Week</option>
                                <option value="month" ${source.timeFilter === 'month' ? 'selected' : ''}>Past Month</option>
                                <option value="year" ${source.timeFilter === 'year' ? 'selected' : ''}>Past Year</option>
                                <option value="all" ${source.timeFilter === 'all' ? 'selected' : ''}>All Time</option>
                            </select>
                        </div>
                    </div>
                    <div class="config-field">
                        <label class="fluent-label">Max Items:</label>
                        <input type="number" class="fluent-input source-input" 
                               data-field="maxItems" value="${window.SafeDOM.finiteNumber(source.maxItems, 5)}" min="1" max="1000">
                    </div>
                    <div class="source-info">
                        <span>Last collected: Never</span>
                        <span>Items found: 0</span>
                    </div>
                `;
                
            case 'github':
                const githubRepos = source.repositories || [];
                const githubRepoList = githubRepos.map((repo, index) => `
                    <div class="repo-item" data-repo-index="${index}">
                        <div class="config-field-row" style="align-items: center;">
                            <label class="fluent-toggle" style="margin: 0;">
                                <input type="checkbox" class="github-repo-toggle" 
                                       data-repo-index="${index}" ${repo.enabled ? 'checked' : ''}>
                                <span class="fluent-toggle-slider"></span>
                            </label>
                            <div style="flex: 1;">
                                <strong>${window.SafeDOM.escapeHtml(repo.owner)}/${window.SafeDOM.escapeHtml(repo.repo)}</strong>
                            </div>
                            <button class="fluent-button-icon btn-remove-github-repo" 
                                    data-repo-index="${index}" 
                                    aria-label="Remove repository"
                                    style="color: var(--error-color);">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>
                    </div>
                `).join('');
                
                return `
                    <div class="config-field">
                        <label class="fluent-label">Repositories:</label>
                        <div class="repo-list" style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;">
                            ${githubRepoList || '<div class="fluent-alert fluent-alert-warning"><i class="bi bi-exclamation-triangle"></i><div>No repositories configured</div></div>'}
                        </div>
                    </div>
                    
                    <div class="config-field" style="border-top: 1px solid var(--border-color); padding-top: 12px;">
                        <label class="fluent-label">Add New Repository:</label>
                        <div class="config-field-row">
                            <input type="text" class="fluent-input" 
                                   id="newGithubRepoOwner" placeholder="Owner (e.g., microsoft)">
                            <input type="text" class="fluent-input" 
                                  id="newGithubRepoName" placeholder="Repository (e.g., SqlClient)">
                            <button class="fluent-button fluent-button-primary btn-add-github-repo">
                                <i class="bi bi-plus"></i> Add
                            </button>
                        </div>
                    </div>
                    
                    <div class="config-field">
                        <label class="fluent-label">Discussion State:</label>
                        <select class="fluent-select source-input" data-field="state">
                            <option value="open" ${source.state === 'open' ? 'selected' : ''}>Open</option>
                            <option value="closed" ${source.state === 'closed' ? 'selected' : ''}>Closed</option>
                            <option value="all" ${source.state === 'all' ? 'selected' : ''}>All</option>
                        </select>
                    </div>
                    <div class="config-field">
                        <label class="fluent-label">Relevant results across repositories:</label>
                        <input type="number" class="fluent-input source-input" 
                               data-field="maxItems" value="${window.SafeDOM.finiteNumber(source.maxItems, 5)}" min="1" max="1000">
                    </div>
                    <div class="fluent-alert fluent-alert-info">
                        <i class="bi bi-info-circle"></i>
                        <div>Note: Repository must have Discussions enabled. All enabled repositories will be collected.</div>
                    </div>
                    <div class="source-info">
                        <span>Repositories: ${githubRepos.filter(r => r.enabled).length} enabled / ${githubRepos.length} total</span>
                    </div>
                `;
                
            case 'githubIssues':
                const repos = source.repositories || [];
                const repoList = repos.map((repo, index) => `
                    <div class="repo-item" data-repo-index="${index}">
                        <div class="config-field-row" style="align-items: center;">
                            <label class="fluent-toggle" style="margin: 0;">
                                <input type="checkbox" class="repo-toggle" 
                                       data-repo-index="${index}" ${repo.enabled ? 'checked' : ''}>
                                <span class="fluent-toggle-slider"></span>
                            </label>
                            <div style="flex: 1;">
                                <strong>${window.SafeDOM.escapeHtml(repo.owner)}/${window.SafeDOM.escapeHtml(repo.repo)}</strong>
                            </div>
                            <button class="fluent-button-icon btn-remove-repo" 
                                    data-repo-index="${index}" 
                                    aria-label="Remove repository"
                                    style="color: var(--error-color);">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>
                    </div>
                `).join('');
                
                return `
                    <div class="config-field">
                        <label class="fluent-label">Repositories:</label>
                        <div class="repo-list" style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;">
                            ${repoList || '<div class="fluent-alert fluent-alert-warning"><i class="bi bi-exclamation-triangle"></i><div>No repositories configured</div></div>'}
                        </div>
                    </div>
                    
                    <div class="config-field" style="border-top: 1px solid var(--border-color); padding-top: 12px;">
                        <label class="fluent-label">Add New Repository:</label>
                        <div class="config-field-row">
                            <input type="text" class="fluent-input" 
                                   id="newRepoOwner" placeholder="Owner (e.g., microsoft)">
                            <input type="text" class="fluent-input" 
                                  id="newRepoName" placeholder="Repository (e.g., vscode-mssql)">
                            <button class="fluent-button fluent-button-primary btn-add-repo">
                                <i class="bi bi-plus"></i> Add
                            </button>
                        </div>
                    </div>
                    
                    <div class="config-field">
                        <label class="fluent-label">Issue State:</label>
                        <select class="fluent-select source-input" data-field="state">
                            <option value="open" ${source.state === 'open' ? 'selected' : ''}>Open</option>
                            <option value="closed" ${source.state === 'closed' ? 'selected' : ''}>Closed</option>
                            <option value="all" ${source.state === 'all' ? 'selected' : ''}>All</option>
                        </select>
                    </div>
                    <div class="config-field">
                        <label class="fluent-label">Relevant results across repositories:</label>
                        <input type="number" class="fluent-input source-input" 
                               data-field="maxItems" value="${window.SafeDOM.finiteNumber(source.maxItems, 5)}" min="1" max="1000">
                    </div>
                    <div class="fluent-alert fluent-alert-info">
                        <i class="bi bi-info-circle"></i>
                        <div>Collects issues only (excludes pull requests). All enabled repositories will be collected.</div>
                    </div>
                    <div class="source-info">
                        <span>Repositories: ${repos.filter(r => r.enabled).length} enabled / ${repos.length} total</span>
                    </div>
                `;
                
            case 'stackoverflow':
            case 'dbaStackExchange':
            case 'microsoftQA':
            case 'techCommunity':
            case 'hackerNews':
            case 'devCommunity':
                const publicSourceDetails = {
                    stackoverflow: 'Uses the public Stack Exchange API for sql-server, azure-sql-database, and azure-sql-managed-instance.',
                    dbaStackExchange: 'Uses the public Stack Exchange API for SQL Server and Azure SQL database administration.',
                    microsoftQA: 'Searches public Microsoft Q&A results scoped to SQL Server.',
                    techCommunity: 'Scans official Microsoft SQL Server and Azure SQL RSS feeds and their public comment feeds.',
                    hackerNews: 'Uses the public Hacker News Algolia API; no account or API key is required.',
                    devCommunity: 'Uses the public DEV/Forem API for SQL Server, Azure SQL, and MSSQL tags.'
                }[sourceId];
                const publicTerms = source.tags || source.queries || source.feeds || [];
                const publicTermsMarkup = publicTerms.length > 0
                    ? `
                        <div class="config-field">
                            <label class="fluent-label">Product filters:</label>
                            <div class="source-info">
                                <span>${publicTerms.map((term) => window.SafeDOM.escapeHtml(term)).join(', ')}</span>
                            </div>
                        </div>
                    `
                    : '';
                const ageMarkup = sourceId === 'hackerNews'
                    ? `
                        <div class="config-field">
                            <label class="fluent-label">Lookback (days):</label>
                            <input type="number" class="fluent-input source-input"
                                   data-field="days" value="${window.SafeDOM.finiteNumber(source.days, 180)}"
                                   min="1" max="3650">
                        </div>
                    `
                    : '';
                return `
                    ${publicTermsMarkup}
                    ${ageMarkup}
                    <div class="config-field">
                        <label class="fluent-label">Max Items:</label>
                        <input type="number" class="fluent-input source-input"
                               data-field="maxItems" value="${window.SafeDOM.finiteNumber(source.maxItems, 5)}"
                               min="1" max="1000">
                    </div>
                    <div class="fluent-alert fluent-alert-info">
                        <i class="bi bi-info-circle"></i>
                        <div>${publicSourceDetails}</div>
                    </div>
                `;

            case 'fabricCommunity':
                return `
                    <div class="config-field">
                        <label class="fluent-label">Max Items:</label>
                        <input type="number" class="fluent-input source-input" 
                               data-field="maxItems" value="${window.SafeDOM.finiteNumber(source.maxItems, 5)}" min="1" max="1000">
                    </div>
                    <div class="fluent-alert fluent-alert-info">
                        <i class="bi bi-info-circle"></i>
                        <div>This source uses default Microsoft Fabric Community forums configuration.</div>
                    </div>
                    <div class="source-info">
                        <span>Last collected: Never</span>
                        <span>Items found: 0</span>
                    </div>
                `;
                
            case 'ado':
                return `
                    <div class="config-field">
                        <label class="fluent-label">Parent Work Item ID:</label>
                        <input type="text" class="fluent-input source-input" 
                               data-field="parentWorkItem" value="${window.SafeDOM.escapeAttribute(source.parentWorkItem)}">
                    </div>
                    <div class="config-field">
                        <label class="fluent-label">Work Item Types:</label>
                        <select class="fluent-select source-input" data-field="workItemTypes" multiple>
                            <option value="Bug" ${source.workItemTypes.includes('Bug') ? 'selected' : ''}>Bug</option>
                            <option value="Feature" ${source.workItemTypes.includes('Feature') ? 'selected' : ''}>Feature</option>
                            <option value="User Story" ${source.workItemTypes.includes('User Story') ? 'selected' : ''}>User Story</option>
                            <option value="Task" ${source.workItemTypes.includes('Task') ? 'selected' : ''}>Task</option>
                        </select>
                    </div>
                    <div class="config-field">
                        <label class="fluent-label">Max Items:</label>
                        <input type="number" class="fluent-input source-input" 
                               data-field="maxItems" value="${window.SafeDOM.finiteNumber(source.maxItems, 5)}" min="1" max="1000">
                    </div>
                    <div class="source-info">
                        <span>Last collected: Never</span>
                        <span>Items found: 0</span>
                    </div>
                `;
                
            default:
                return '<div class="fluent-alert fluent-alert-warning">No configuration available for this source.</div>';
        }
    }
    
    renderSettings() {
        const container = document.getElementById('collectionSettings');
        if (!container) return;
        
        container.innerHTML = `
            <div class="config-field-row">
                <div class="config-field">
                    <label class="fluent-label">Lookback:</label>
                    <select class="fluent-select setting-input" data-field="timeRangeMonths">
                        <option value="1" ${this.settings.timeRangeMonths === 1 ? 'selected' : ''}>Last month</option>
                        <option value="3" ${this.settings.timeRangeMonths === 3 ? 'selected' : ''}>Last 3 months</option>
                        <option value="6" ${this.settings.timeRangeMonths === 6 ? 'selected' : ''}>Last 6 months</option>
                        <option value="12" ${this.settings.timeRangeMonths === 12 ? 'selected' : ''}>Last year</option>
                        <option value="24" ${this.settings.timeRangeMonths === 24 ? 'selected' : ''}>Last 2 years</option>
                        <option value="0" ${this.settings.timeRangeMonths === 0 ? 'selected' : ''}>All time</option>
                    </select>
                </div>
                <div class="config-field">
                    <label class="fluent-label">Relevant results per source:</label>
                    <select class="fluent-select setting-input" data-field="maxItemsPerSource">
                        <option value="25" ${parseInt(this.settings.maxItemsPerSource) === 25 ? 'selected' : ''}>25</option>
                        <option value="50" ${parseInt(this.settings.maxItemsPerSource) === 50 ? 'selected' : ''}>50</option>
                        <option value="100" ${parseInt(this.settings.maxItemsPerSource) === 100 ? 'selected' : ''}>100</option>
                        <option value="250" ${parseInt(this.settings.maxItemsPerSource) === 250 ? 'selected' : ''}>250</option>
                        <option value="500" ${parseInt(this.settings.maxItemsPerSource) === 500 ? 'selected' : ''}>500</option>
                        <option value="1000" ${parseInt(this.settings.maxItemsPerSource) === 1000 ? 'selected' : ''}>1,000</option>
                    </select>
                </div>
                <div class="config-field">
                    <label class="fluent-label">Candidate depth:</label>
                    <select class="fluent-select setting-input" data-field="candidateMultiplier">
                        <option value="5" ${parseInt(this.settings.candidateMultiplier) === 5 ? 'selected' : ''}>Balanced (5×)</option>
                        <option value="10" ${parseInt(this.settings.candidateMultiplier) === 10 ? 'selected' : ''}>Thorough (10×)</option>
                        <option value="25" ${parseInt(this.settings.candidateMultiplier) === 25 ? 'selected' : ''}>Maximum recall (25×)</option>
                    </select>
                </div>
            </div>
            <div class="config-field" style="margin-top: 12px;">
                <label class="fluent-label">
                    <input type="checkbox" class="fluent-checkbox setting-input" 
                           data-field="includeReplies" ${this.settings.includeReplies !== false ? 'checked' : ''}>
                    Include comments, answers, and replies when supported
                </label>
            </div>
            <div class="config-field">
                <label class="fluent-label">
                    <input type="checkbox" class="fluent-checkbox setting-input" 
                           data-field="respectRateLimits" ${this.settings.respectRateLimits ? 'checked' : ''}>
                    Honor source rate limits and retry guidance
                </label>
            </div>
        `;
    }
    
    renderKeywordsInSettings() {
        // This would update the keywords display in settings if needed
        console.log('Keywords loaded:', this.settings.keywords);
    }
    
    getCollectionConfig() {
        // Return configuration in the format expected by the backend
        return {
            sources: Object.keys(this.sources).reduce((acc, key) => {
                if (this.sources[key].enabled) {
                    acc[key] = this.sources[key];
                }
                return acc;
            }, {}),
            settings: this.settings
        };
    }
    
    resetToDefaults() {
        if (confirm('Are you sure you want to reset all source configurations to defaults?')) {
            localStorage.removeItem('feedbackCollectorConfig');
            location.reload();
        }
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.sourceConfigManager = new SourceConfigManager();
});

// Export for use in other scripts
window.SourceConfigManager = SourceConfigManager;
