/**
 * CloudOdoo - Frontend Interaction JavaScript
 * Handles client-side interactions for the SaaS website.
 * All data comes from server-rendered QWeb templates.
 */

// ============================================
// Utility Functions
// ============================================

const CloudOdoo = {
    // Debounce function
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    },

    // Show toast notification
    showToast(message, type = 'info') {
        let container = document.getElementById('co-toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'co-toast-container';
            container.style.cssText = 'position:fixed;top:100px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px;';
            document.body.appendChild(container);
        }
        const iconMap = { success: 'check-circle', error: 'exclamation-circle', info: 'info-circle' };
        const toast = document.createElement('div');
        toast.className = `toast-notification toast-${type} fade-in`;
        toast.innerHTML = `<i class="fas fa-${iconMap[type] || 'info-circle'}"></i><span>${message}</span>`;
        container.appendChild(toast);
        setTimeout(() => {
            toast.classList.add('fade-out');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    },

    // Format currency
    formatCurrency(amount, currency) {
        currency = currency || 'USD';
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: currency,
            minimumFractionDigits: 0,
            maximumFractionDigits: 2
        }).format(amount);
    },

    // Format storage (MB to human readable)
    formatStorage(mb) {
        if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
        return mb + ' MB';
    },

    // Show styled confirmation modal (programmatic use)
    showConfirm(message, variant, yesText, onConfirm) {
        initConfirmModals();
        _coSetupModal(message, variant, yesText, 'No, keep it', onConfirm);
    },

    // Get CSRF token from the page
    getCsrfToken() {
        const el = document.querySelector('input[name="csrf_token"]');
        return el ? el.value : '';
    },

    // Odoo JSON-RPC call
    jsonRpc(url, params) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                jsonrpc: '2.0',
                id: Math.floor(Math.random() * 1000000),
                method: 'call',
                params: params || {},
            }),
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) throw new Error(data.error.data?.message || data.error.message || 'Server error');
            return data.result;
        });
    },
};

// ============================================
// Theme Toggle (Dark/Light) — REMOVED
// ============================================
//
// The legacy toggle here used its own `co-theme` localStorage key and
// `#themeToggle`/`#themeIcon` elements (no longer rendered). Worse, it
// ran late (on initAll, after the page had loaded) and unconditionally
// did `setAttribute('data-theme', localStorage.getItem('co-theme') ||
// 'dark')` — and because the live theme system writes `veltnex-theme`,
// not `co-theme`, that read was ALWAYS 'dark'. So every QWeb page got
// its correct server-rendered theme stomped back to dark a beat after
// load (e.g. toggle light in the SPA → navigate to a QWeb page → it
// briefly showed light, then flipped to dark).
//
// Theming is now owned entirely by the `veltnex-theme` system:
//   - server-side  : cloudodoo_html_theme / spa.py stamp <html data-theme>
//   - pre-paint    : the inline script in cloudodoo_theme_init
//   - assets/sync  : veltnex_theme.js (wires .vx-theme-toggle)
// Do NOT reintroduce a second toggle here.


// ============================================
// Subdomain Validation (Configure Page)
// ============================================

function initSubdomainCheck() {
    const input = document.getElementById('subdomain-input');
    if (!input) return;

    const feedback = document.getElementById('subdomain-feedback');
    const preview = document.getElementById('url-preview');
    const domainSelect = document.getElementById('domain-select');
    const submitBtn = document.getElementById('submit-btn');
    const subdomainRe = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

    function updatePreview() {
        const sub = input.value.toLowerCase() || 'your-subdomain';
        const domain = domainSelect ? domainSelect.options[domainSelect.selectedIndex].text : '';
        if (preview) {
            preview.innerHTML = '<span class="subdomain">' + sub + '</span>.' + domain;
        }
    }

    const checkAvailability = CloudOdoo.debounce(() => {
        const value = input.value.toLowerCase().trim();

        if (!value) {
            feedback.innerHTML = '';
            input.classList.remove('is-valid', 'is-invalid');
            if (submitBtn) submitBtn.disabled = true;
            return;
        }

        if (!subdomainRe.test(value)) {
            input.classList.remove('is-valid');
            input.classList.add('is-invalid');
            feedback.innerHTML = '<span class="invalid-feedback d-block"><i class="fas fa-times me-1"></i>Invalid format. Use lowercase letters, numbers, and hyphens only.</span>';
            if (submitBtn) submitBtn.disabled = true;
            return;
        }

        // Show loading
        feedback.innerHTML = '<span class="text-muted"><span class="spinner-sm me-2"></span>Checking availability...</span>';

        const domainId = domainSelect ? parseInt(domainSelect.value) : 0;
        CloudOdoo.jsonRpc('/saas/check-subdomain', {
            subdomain: value,
            domain_id: domainId,
        }).then(result => {
            if (result.available) {
                input.classList.remove('is-invalid');
                input.classList.add('is-valid');
                feedback.innerHTML = '<span class="valid-feedback d-block"><i class="fas fa-check me-1"></i>' + result.message + '</span>';
                if (submitBtn) submitBtn.disabled = false;
            } else {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
                feedback.innerHTML = '<span class="invalid-feedback d-block"><i class="fas fa-times me-1"></i>' + (result.message || 'Subdomain not available') + '</span>';
                if (submitBtn) submitBtn.disabled = true;
            }
        }).catch(() => {
            feedback.innerHTML = '<span class="invalid-feedback d-block"><i class="fas fa-times me-1"></i>Error checking availability. Please try again.</span>';
            if (submitBtn) submitBtn.disabled = true;
        });
    }, 500);

    input.addEventListener('input', () => {
        input.value = input.value.toLowerCase().replace(/[^a-z0-9-]/g, '');
        updatePreview();
        checkAvailability();
    });

    if (domainSelect) {
        domainSelect.addEventListener('change', () => {
            updatePreview();
            // Re-check if there's already a value
            if (input.value.trim()) checkAvailability();
        });
    }

    updatePreview();
}

// ============================================
// OTP Input Handling (Registration Page)
// ============================================

function initOTPInputs() {
    const inputs = document.querySelectorAll('.otp-input');
    if (!inputs.length) return;

    inputs.forEach((input, index) => {
        input.addEventListener('input', (e) => {
            const value = e.target.value;
            if (value && index < inputs.length - 1) {
                inputs[index + 1].focus();
            }
            // Update hidden field with combined OTP
            updateOTPHidden();
        });

        input.addEventListener('keydown', (e) => {
            if (e.key === 'Backspace' && !input.value && index > 0) {
                inputs[index - 1].focus();
            }
        });

        input.addEventListener('paste', (e) => {
            e.preventDefault();
            const paste = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 6);
            [...paste].forEach((char, i) => {
                if (inputs[i]) inputs[i].value = char;
            });
            updateOTPHidden();
        });
    });

    if (inputs[0]) inputs[0].focus();

    function updateOTPHidden() {
        const hidden = document.getElementById('phone_otp_hidden');
        if (hidden) {
            hidden.value = [...inputs].map(i => i.value).join('');
        }
    }
}

// ============================================
// OTP Countdown Timer
// ============================================

function initOTPTimer() {
    const timerEl = document.getElementById('otp-timer');
    if (!timerEl) return;

    let remaining = parseInt(timerEl.dataset.seconds || '600');

    const interval = setInterval(() => {
        remaining--;
        const minutes = Math.floor(remaining / 60);
        const seconds = remaining % 60;
        timerEl.textContent = minutes + ':' + seconds.toString().padStart(2, '0');

        if (remaining <= 0) {
            clearInterval(interval);
            timerEl.textContent = 'Expired';
        }
    }, 1000);
}

// ============================================
// Password Strength Indicator (Registration)
// ============================================

function initPasswordStrength() {
    const password = document.getElementById('password');
    const confirm = document.getElementById('confirm-password');
    const strengthBar = document.getElementById('strength-bar');
    const matchFeedback = document.getElementById('password-match');

    if (!password || !strengthBar) return;

    password.addEventListener('input', () => {
        const value = password.value;
        let strength = 0;
        if (value.length >= 8) strength++;
        if (/[a-z]/.test(value) && /[A-Z]/.test(value)) strength++;
        if (/\d/.test(value)) strength++;
        if (/[^a-zA-Z0-9]/.test(value)) strength++;

        strengthBar.className = 'strength-bar';
        if (strength <= 1) strengthBar.classList.add('strength-weak');
        else if (strength <= 2) strengthBar.classList.add('strength-medium');
        else strengthBar.classList.add('strength-strong');

        checkMatch();
    });

    if (confirm) {
        confirm.addEventListener('input', checkMatch);
    }

    function checkMatch() {
        if (!confirm || !matchFeedback || !confirm.value) {
            if (matchFeedback) matchFeedback.innerHTML = '';
            return;
        }
        if (password.value === confirm.value) {
            matchFeedback.innerHTML = '<span class="valid-feedback d-block"><i class="fas fa-check me-1"></i>Passwords match</span>';
        } else {
            matchFeedback.innerHTML = '<span class="invalid-feedback d-block"><i class="fas fa-times me-1"></i>Passwords do not match</span>';
        }
    }
}



// ============================================
// Loading overlay
// ============================================
// Blocking spinner that covers the screen while a long-running form
// submit is in flight. Triggered by ``initOverlayOnSubmit``: when a form
// with ``data-loading-text`` is submitted, the overlay shows immediately
// so the user can't double-click before the redirect lands.

function showLoadingOverlay(text, subtitle) {
    var existing = document.getElementById('saas-loading-overlay');
    if (existing) {
        // Already showing — just refresh the main text if requested.
        if (text) {
            var t = existing.querySelector('.saas-loading-overlay-text-main');
            if (t) t.textContent = text;
        }
        return existing;
    }
    var overlay = document.createElement('div');
    overlay.id = 'saas-loading-overlay';
    overlay.className = 'saas-loading-overlay';
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.innerHTML = (
        '<div class="spinner-border text-light" role="status">' +
        '<span class="visually-hidden">Loading…</span></div>' +
        '<div class="saas-loading-overlay-text">' +
        '<div class="saas-loading-overlay-text-main"></div>' +
        '<div class="saas-loading-overlay-subtitle"></div>' +
        '</div>'
    );
    overlay.querySelector('.saas-loading-overlay-text-main').textContent =
        text || 'Working on it…';
    overlay.querySelector('.saas-loading-overlay-subtitle').textContent =
        subtitle || 'This page will refresh automatically when it\'s done.';
    document.body.appendChild(overlay);
    document.body.classList.add('saas-overlay-locked');
    return overlay;
}



function _setButtonLoading(btn, text) {
    if (btn.dataset.saasOriginalContent === undefined) {
        btn.dataset.saasOriginalContent = btn.innerHTML;
    }
    btn.innerHTML = (
        '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
        (text || 'Working…')
    );
    btn.disabled = true;
}

function _lockFormButtons(form) {
    // Disable every other interactive button in the form so the
    // customer can't double-click or trigger another action while
    // this one is in flight.
    var buttons = form.querySelectorAll('button, input[type="submit"]');
    buttons.forEach(function(b) { b.disabled = true; });
}

function initOverlayOnSubmit() {
    document.addEventListener('submit', function(e) {
        var form = e.target;
        if (!form || !form.matches || !form.matches('form[data-loading-text]')) {
            return;
        }
        // Forms that explicitly opt out (e.g. dismiss buttons) get
        // neither the overlay nor the per-button loading state.
        if (form.dataset.loadingSkip === '1') return;
        var text = form.getAttribute('data-loading-text');
        var subtitle = form.getAttribute('data-loading-subtitle');
        // Button-only mode for the quick / per-row actions where a
        // full-screen freeze would feel heavy-handed. The submit
        // button shows the spinner, the rest of the form is disabled,
        // and the existing inline banner / polling on the next page
        // load handles the "still working" state.
        if (form.dataset.loadingButtonOnly === '1') {
            var submitter = e.submitter
                || form.querySelector('button[type="submit"], input[type="submit"]');
            if (submitter) _setButtonLoading(submitter, text);
            _lockFormButtons(form);
            return;
        }
        // Full-screen overlay for the heavier flows (create database,
        // repair feature, restore snapshot, …).
        showLoadingOverlay(text, subtitle);
    }, true);
}






// ============================================
// Global Form Loading States
// ============================================

function initFormLoadingStates() {
    document.addEventListener('submit', function(e) {
        var form = e.target;
        // Skip forms handled by other systems
        if (form.id === 'login-form') return;
        if (form.closest('.o_payment_form')) return;
        if (form.closest('#payment_method')) return;

        var btn = form.querySelector('button[type="submit"]:not([disabled])');
        if (!btn) btn = form.querySelector('button[name]:not([disabled])');
        if (!btn) return;

        btn.dataset.originalHtml = btn.innerHTML;
        btn.disabled = true;
        var loadingText = btn.dataset.loadingText || 'Processing...';
        btn.innerHTML = '<span class="spinner-sm me-2" style="display:inline-block;vertical-align:middle;"></span>' + loadingText;
    });
}

// ============================================
// Instance Folders
// ============================================

var _coInputOnConfirm = null;






// ============================================
// Shared Slider Helpers
// ============================================

function generateTicks(containerId, min, max, step) {
    var container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';
    for (var i = min; i <= max; i += step) {
        var tick = document.createElement('div');
        tick.className = 'slider-tick';
        var pct = max > min ? ((i - min) / (max - min)) * 100 : 0;
        tick.style.left = pct + '%';
        tick.innerHTML = '<span class="tick-mark"></span><span class="tick-label">' + i + '</span>';
        if ((containerId === 'workers-ticks' || containerId === 'upgrade-workers-ticks') && i >= 4 && i <= 6) {
            tick.classList.add('tick-recommended');
        }
        container.appendChild(tick);
    }
}

function generateStorageTicks(containerId, min, max) {
    var container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';
    // Build tick list: always include min and max, plus well-spaced milestones
    var candidates = [min, 25, 50, 100, 150, 200, max];
    var ticks = [];
    var range = max - min;
    // Minimum distance between ticks: 10% of range to avoid label overlap
    var minGap = range * 0.1;

    for (var i = 0; i < candidates.length; i++) {
        var val = candidates[i];
        if (val < min || val > max) continue;
        // Check distance from last tick
        if (ticks.length > 0 && (val - ticks[ticks.length - 1]) < minGap) continue;
        ticks.push(val);
    }
    // Always ensure max is included
    if (ticks[ticks.length - 1] !== max) {
        // Remove last tick if too close to max
        if (ticks.length > 1 && (max - ticks[ticks.length - 1]) < minGap) {
            ticks.pop();
        }
        ticks.push(max);
    }

    ticks.forEach(function(val) {
        var tick = document.createElement('div');
        tick.className = 'slider-tick';
        var pct = range > 0 ? ((val - min) / range) * 100 : 0;
        tick.style.left = pct + '%';
        tick.innerHTML = '<span class="tick-mark"></span><span class="tick-label">' + val + 'GB</span>';
        container.appendChild(tick);
    });
}


// ============================================
// Upgrade / Change Plan Builder
// ============================================

function initUpgradePlanBuilder() {
    var configEl = document.getElementById('upgrade-plan-config');
    if (!configEl) return;

    var config = {
        // Pricing is computed SERVER-SIDE by the pricing engine — the
        // browser never sees per-unit rates. workers/storage are POSTed
        // to calcUrl and the returned total is rendered.
        calcUrl: configEl.dataset.calcUrl || '/saas/hosting-plan/calculate',
        minWorkers: parseInt(configEl.dataset.minWorkers) || 2,
        maxWorkers: parseInt(configEl.dataset.maxWorkers) || 8,
        minStorage: parseInt(configEl.dataset.minStorage) || 5,
        maxStorage: parseInt(configEl.dataset.maxStorage) || 200,
        yearlyDiscountPct: parseInt(configEl.dataset.yearlyDiscountPct) || 20,
        currency: configEl.dataset.currency || 'USD',
        mode: configEl.dataset.mode || 'upgrade',
        // The instance's pinned region (0 = none -> x1.0). Sent to the
        // calc endpoint so the preview is region-scaled exactly like the
        // plan that will be created.
        regionId: parseInt(configEl.dataset.regionId) || 0,
        currentWorkers: parseInt(configEl.dataset.currentWorkers) || 0,
        currentStorage: parseInt(configEl.dataset.currentStorage) || 0,
        currentBilling: configEl.dataset.currentBilling || 'monthly',
        minBackups: parseInt(configEl.dataset.minBackups) || 3,
        maxBackups: parseInt(configEl.dataset.maxBackups) || 14,
    };

    var workersSlider = document.getElementById('upgrade-workers-slider');
    var storageSlider = document.getElementById('upgrade-storage-slider');
    if (!workersSlider || !storageSlider) return;

    var currentBilling = config.mode === 'change' ? config.currentBilling : 'monthly';
    var YEARLY_DISCOUNT = config.yearlyDiscountPct / 100;

    // Generate tick marks
    generateTicks('upgrade-workers-ticks', config.minWorkers, config.maxWorkers, 1);
    generateStorageTicks('upgrade-storage-ticks', config.minStorage, config.maxStorage);

    function updateSliderTrack(slider) {
        var min = parseFloat(slider.min);
        var max = parseFloat(slider.max);
        var val = parseFloat(slider.value);
        var pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
        slider.style.setProperty('--slider-pct', pct + '%');
    }

    function setText(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    // Fetch the authoritative price from the server (pricing engine).
    // Debounced so dragging a slider doesn't spam the endpoint. The
    // browser never computes the price itself.
    var _priceTimer = null;
    var _priceSeq = 0;
    function refreshPrice(workers, storage) {
        var monthlyEl = document.getElementById('upgrade-summary-monthly-total');
        var yearlyEl = document.getElementById('upgrade-summary-yearly-total');
        if (monthlyEl) monthlyEl.textContent = '…';
        if (yearlyEl) yearlyEl.textContent = '…';
        if (_priceTimer) clearTimeout(_priceTimer);
        var seq = ++_priceSeq;
        _priceTimer = setTimeout(function () {
            CloudOdoo.jsonRpc(config.calcUrl, {
                workers: workers, storage: storage,
                region: config.regionId || undefined,
            })
                .then(function (r) {
                    if (seq !== _priceSeq) return;  // a newer request superseded this
                    var monthly = (r && r.monthly_total) || 0;
                    var yearlyFull = monthly * 12;
                    // Use the server's region-scaled + discounted yearly
                    // total; fall back to the local discount only if absent.
                    var yearlyDiscounted = (r && r.yearly_total != null)
                        ? r.yearly_total
                        : yearlyFull * (1 - YEARLY_DISCOUNT);
                    var yearlySavings = yearlyFull - yearlyDiscounted;
                    var cur = (r && r.currency) || config.currency;
                    if (currentBilling === 'yearly') {
                        setText('upgrade-summary-yearly-total', CloudOdoo.formatCurrency(yearlyDiscounted, cur));
                        setText('upgrade-summary-yearly-savings', CloudOdoo.formatCurrency(yearlySavings, cur));
                    } else {
                        setText('upgrade-summary-monthly-total', CloudOdoo.formatCurrency(monthly, cur));
                    }
                })
                .catch(function () {
                    if (seq !== _priceSeq) return;
                    if (monthlyEl) monthlyEl.textContent = '—';
                    if (yearlyEl) yearlyEl.textContent = '—';
                });
        }, 250);
    }

    function calculateAndUpdate() {
        var workers = parseInt(workersSlider.value);
        var storage = parseInt(storageSlider.value);

        // Sync number inputs with slider values
        var wInput = document.getElementById('upgrade-workers-input');
        var sInput = document.getElementById('upgrade-storage-input');
        if (wInput && wInput !== document.activeElement) wInput.value = workers;
        if (sInput && sInput !== document.activeElement) sInput.value = storage;

        // Update resource summary (counts only — per-resource pricing
        // is an internal calculation and is intentionally not shown
        // to the customer; they see the final total only).
        setText('upgrade-summary-workers', workers);
        setText('upgrade-summary-storage', storage);

        // Toggle monthly/yearly display
        var monthlyDisplay = document.getElementById('upgrade-summary-monthly-display');
        var yearlyDisplay = document.getElementById('upgrade-summary-yearly-display');
        if (currentBilling === 'yearly') {
            if (monthlyDisplay) monthlyDisplay.style.display = 'none';
            if (yearlyDisplay) yearlyDisplay.style.display = '';
        } else {
            if (monthlyDisplay) monthlyDisplay.style.display = '';
            if (yearlyDisplay) yearlyDisplay.style.display = 'none';
        }

        // The price (monthly/yearly totals) comes from the server.
        refreshPrice(workers, storage);

        // Backups are now a separate paid add-on (configured from the
        // instance's Snapshots page after deployment), not bundled
        // with the plan — so no backup-count line on this builder.

        // Update hidden form fields
        var formWorkers = document.getElementById('upgrade-form-workers');
        var formStorage = document.getElementById('upgrade-form-storage');
        var formBilling = document.getElementById('upgrade-form-billing');
        if (formWorkers) formWorkers.value = workers;
        if (formStorage) formStorage.value = storage;
        if (formBilling) formBilling.value = currentBilling;

        // Update slider tracks
        updateSliderTrack(workersSlider);
        updateSliderTrack(storageSlider);

        // Change-plan mode: update action info and submit button
        if (config.mode === 'change') {
            var submitBtn = document.getElementById('upgrade-submit-btn');
            var actionInfo = document.getElementById('upgrade-action-info');
            var reductionNotice = document.getElementById('upgrade-worker-reduction-notice');
            var hasChanges = (workers !== config.currentWorkers ||
                              storage !== config.currentStorage ||
                              currentBilling !== config.currentBilling);

            if (submitBtn) submitBtn.disabled = !hasChanges;

            // Show worker reduction notice
            if (reductionNotice) {
                reductionNotice.style.display = workers < config.currentWorkers ? '' : 'none';
            }

            // Update action info banner
            if (actionInfo) {
                if (!hasChanges) {
                    actionInfo.style.display = 'none';
                } else if (workers < config.currentWorkers) {
                    // Downgrade (worker reduction)
                    actionInfo.style.display = '';
                    actionInfo.style.background = 'rgba(245,158,11,0.06)';
                    actionInfo.style.border = '1px solid rgba(245,158,11,0.15)';
                    actionInfo.innerHTML =
                        '<div class="fw-semibold mb-1" style="color:#F59E0B;">' +
                        '<i class="fas fa-calendar-alt me-1"></i>Scheduled at Next Billing</div>' +
                        '<div style="color:var(--co-text-secondary,#A1A1AA);">' +
                        'Worker reduction will take effect at your next billing cycle. ' +
                        'You keep your current resources until then.</div>';
                    if (submitBtn) {
                        submitBtn.className = 'btn btn-warning btn-lg w-100';
                        submitBtn.innerHTML = '<i class="fas fa-calendar-alt me-2"></i>Schedule Change';
                    }
                } else {
                    // Upgrade (workers same/increased, storage increased)
                    actionInfo.style.display = '';
                    actionInfo.style.background = 'rgba(16,185,129,0.06)';
                    actionInfo.style.border = '1px solid rgba(16,185,129,0.15)';
                    actionInfo.innerHTML =
                        '<div class="fw-semibold mb-1" style="color:#10B981;">' +
                        '<i class="fas fa-bolt me-1"></i>Immediate Upgrade</div>' +
                        '<div style="color:var(--co-text-secondary,#A1A1AA);">' +
                        'Resources will be upgraded instantly with zero downtime. ' +
                        'You\'ll be charged the prorated difference.</div>';
                    if (submitBtn) {
                        submitBtn.className = 'btn btn-primary btn-lg w-100';
                        submitBtn.innerHTML = '<i class="fas fa-arrow-up me-2"></i>Upgrade Now';
                    }
                }
            }
        }
    }

    // Slider event listeners
    workersSlider.addEventListener('input', calculateAndUpdate);
    storageSlider.addEventListener('input', calculateAndUpdate);

    // Number input listeners — sync to sliders
    var upgradeWorkersInput = document.getElementById('upgrade-workers-input');
    var upgradeStorageInput = document.getElementById('upgrade-storage-input');
    if (upgradeWorkersInput) {
        upgradeWorkersInput.addEventListener('input', function() {
            var v = Math.max(config.minWorkers, Math.min(config.maxWorkers, parseInt(this.value) || config.minWorkers));
            workersSlider.value = v;
            calculateAndUpdate();
        });
    }
    if (upgradeStorageInput) {
        upgradeStorageInput.addEventListener('input', function() {
            var v = Math.max(config.minStorage, Math.min(config.maxStorage, parseInt(this.value) || config.minStorage));
            storageSlider.value = v;
            calculateAndUpdate();
        });
    }

    // Billing toggle
    var toggleContainer = document.getElementById('upgrade-billing-toggle');
    if (toggleContainer) {
        toggleContainer.querySelectorAll('.toggle-btn').forEach(function(btn) {
            if (btn.dataset.billing === currentBilling) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
            btn.addEventListener('click', function() {
                toggleContainer.querySelectorAll('.toggle-btn').forEach(function(b) {
                    b.classList.remove('active');
                });
                btn.classList.add('active');
                currentBilling = btn.dataset.billing;
                calculateAndUpdate();
            });
        });
    }

    // Initial calculation
    calculateAndUpdate();
}


// ============================================
// Page Initialization
// ============================================

function initAll() {
    // Initialize all interactive components
    // (theme is handled by the veltnex-theme system, not here — see the
    //  removed initThemeToggle note above)
    initSubdomainCheck();
    initUpgradePlanBuilder();
    initOTPInputs();
    initOTPTimer();
    initPasswordStrength();
    initOverlayOnSubmit();
    initConfirmModals();
    initFormLoadingStates();

    // Update nav active state
    const path = window.location.pathname;
    document.querySelectorAll('.navbar-nav .nav-link').forEach(link => {
        const href = link.getAttribute('href');
        if (href && path.startsWith(href) && href !== '/') {
            link.classList.add('active');
        }
    });
}

// Run immediately if DOM is already ready (Odoo deferred scripts),
// otherwise wait for DOMContentLoaded.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
} else {
    initAll();
}

// ============================================
// Styled Confirmation Modal (pure CSS/JS, no
// Bootstrap Modal dependency)
// ============================================

var _coModalOnConfirm = null;

function _coShowModal() {
    var m = document.getElementById('co-confirm-modal');
    var b = document.getElementById('co-confirm-backdrop');
    if (!b) {
        b = document.createElement('div');
        b.id = 'co-confirm-backdrop';
        document.body.appendChild(b);
    }
    b.style.display = 'block';
    m.style.display = 'flex';
    requestAnimationFrame(function() {
        b.classList.add('co-show');
        m.classList.add('co-show');
    });
}

function _coHideModal() {
    var m = document.getElementById('co-confirm-modal');
    var b = document.getElementById('co-confirm-backdrop');
    m.classList.remove('co-show');
    if (b) b.classList.remove('co-show');
    setTimeout(function() {
        m.style.display = 'none';
        if (b) b.style.display = 'none';
    }, 200);
    _coModalOnConfirm = null;
}

function _coSetupModal(message, variant, confirmText, cancelText, onConfirm) {
    variant = variant || 'warning';
    confirmText = confirmText || 'Yes, confirm';
    cancelText = cancelText || 'No, keep it';

    var iconMap = {
        warning: { bg: 'rgba(245,158,11,0.15)', color: '#F59E0B', icon: 'fa-exclamation-triangle' },
        danger:  { bg: 'rgba(239,68,68,0.15)',  color: '#EF4444', icon: 'fa-trash-alt' },
        info:    { bg: 'rgba(59,130,246,0.15)',  color: '#3B82F6', icon: 'fa-info-circle' },
    };
    var titleMap = { warning: 'Are you sure?', danger: 'Are you sure?', info: 'Please confirm' };
    var s = iconMap[variant] || iconMap.warning;
    var btnClass = variant === 'danger' ? 'btn-danger' : variant === 'info' ? 'btn-primary' : 'btn-warning';

    var iconEl = document.getElementById('co-confirm-icon');
    iconEl.style.background = s.bg;
    iconEl.style.color = s.color;
    iconEl.innerHTML = '<i class="fas ' + s.icon + '"></i>';
    document.getElementById('co-confirm-title').textContent = titleMap[variant] || 'Confirm';
    document.getElementById('co-confirm-message').textContent = message;
    document.getElementById('co-confirm-yes').className = 'btn ' + btnClass;
    document.getElementById('co-confirm-yes').innerHTML = '<i class="fas fa-check me-1"></i>' + confirmText;
    document.getElementById('co-confirm-no').innerHTML = '<i class="fas fa-times me-1"></i>' + cancelText;

    _coModalOnConfirm = onConfirm;
    _coShowModal();
}

function initConfirmModals() {
    if (document.getElementById('co-confirm-modal')) return;

    // Inject modal HTML — pure CSS positioning, no Bootstrap Modal
    document.body.insertAdjacentHTML('beforeend', `
    <div id="co-confirm-modal" class="co-modal-overlay">
      <div class="co-modal-dialog">
        <div class="co-modal-content">
          <div class="co-modal-header">
            <div style="display:flex;align-items:center;gap:12px;">
              <div id="co-confirm-icon" class="co-modal-icon"></div>
              <h5 id="co-confirm-title" style="margin:0;font-size:1.1rem;">Confirm</h5>
            </div>
            <button type="button" class="co-modal-close" id="co-confirm-close">&times;</button>
          </div>
          <div class="co-modal-body">
            <p id="co-confirm-message" style="margin:0;"></p>
          </div>
          <div class="co-modal-footer">
            <button type="button" class="btn btn-outline-secondary" id="co-confirm-no">
              <i class="fas fa-times me-1"></i>No, keep it
            </button>
            <button type="button" class="btn" id="co-confirm-yes">
              <i class="fas fa-check me-1"></i>Yes, confirm
            </button>
          </div>
        </div>
      </div>
    </div>`);

    // Wire up close/cancel buttons
    document.getElementById('co-confirm-close').addEventListener('click', _coHideModal);
    document.getElementById('co-confirm-no').addEventListener('click', _coHideModal);
    document.getElementById('co-confirm-yes').addEventListener('click', function() {
        // Grab callback BEFORE hiding (hide clears it)
        var fn = _coModalOnConfirm;
        _coHideModal();
        if (fn) {
            setTimeout(fn, 50);
        }
    });

    // Close on backdrop click
    document.getElementById('co-confirm-modal').addEventListener('click', function(e) {
        if (e.target === this) _coHideModal();
    });

    // Close on Escape
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && document.getElementById('co-confirm-modal').classList.contains('co-show')) {
            _coHideModal();
        }
    });

    // Intercept all elements with data-confirm attribute
    document.addEventListener('click', function(e) {
        var trigger = e.target.closest('[data-confirm]');
        if (!trigger) return;

        e.preventDefault();
        e.stopPropagation();

        _coSetupModal(
            trigger.dataset.confirm,
            trigger.dataset.confirmVariant || 'warning',
            trigger.dataset.confirmYes || 'Yes, confirm',
            trigger.dataset.confirmNo || 'No, keep it',
            function() {
                if (trigger.type === 'submit' || trigger.tagName === 'BUTTON') {
                    var form = trigger.closest('form');
                    if (form) {
                        // ``requestSubmit`` (vs ``submit``) fires the
                        // submit event, which our loading-overlay
                        // handler listens for. Without it, confirmed
                        // long-running actions would slip through
                        // without freezing the screen.
                        if (typeof form.requestSubmit === 'function') {
                            form.requestSubmit();
                        } else {
                            form.submit();
                        }
                    }
                } else if (trigger.href) {
                    window.location.href = trigger.href;
                }
            }
        );
    });
}

// ============================================
// Injected Styles (toast + modal)
// ============================================

(function() {
    const style = document.createElement('style');
    style.textContent = `
        .toast-notification {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            padding: 1rem 1.25rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
            min-width: 280px;
            box-shadow: var(--shadow-lg);
            color: var(--text-primary);
        }
        .toast-success { border-color: var(--success); }
        .toast-success i { color: var(--success); }
        .toast-error { border-color: var(--danger); }
        .toast-error i { color: var(--danger); }
        .toast-info i { color: var(--info); }
        .fade-out { opacity: 0; transform: translateX(20px); transition: all 0.3s ease; }

        /* Confirmation modal (pure CSS, no Bootstrap Modal) */
        .co-modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            z-index: 10000;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.2s ease;
        }
        .co-modal-overlay.co-show { opacity: 1; }
        #co-confirm-backdrop {
            display: none;
            position: fixed;
            inset: 0;
            z-index: 9999;
            background: rgba(0,0,0,0.6);
            backdrop-filter: blur(4px);
            opacity: 0;
            transition: opacity 0.2s ease;
        }
        #co-confirm-backdrop.co-show { opacity: 1; }
        .co-modal-dialog {
            max-width: 440px;
            width: 90%;
            margin: auto;
        }
        .co-modal-content {
            background: var(--bg-card, #111113);
            border: 1px solid var(--border-color, #27272A);
            border-radius: var(--radius-lg, 14px);
            color: var(--text-primary, #FAFAFA);
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            transform: translateY(10px) scale(0.98);
            transition: transform 0.2s ease;
        }
        .co-modal-overlay.co-show .co-modal-content {
            transform: translateY(0) scale(1);
        }
        .co-modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1.25rem 1.5rem;
            border-bottom: 1px solid var(--border-color, #27272A);
        }
        .co-modal-icon {
            width: 42px; height: 42px;
            border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.1rem; flex-shrink: 0;
        }
        .co-modal-close {
            background: none; border: none; color: var(--text-muted, #71717A);
            font-size: 1.5rem; cursor: pointer; padding: 0; line-height: 1;
        }
        .co-modal-close:hover { color: var(--text-primary, #FAFAFA); }
        .co-modal-body {
            padding: 1.5rem;
            color: var(--text-secondary, #A1A1AA);
            font-size: 0.95rem; line-height: 1.5;
        }
        .co-modal-footer {
            display: flex; justify-content: flex-end; gap: 0.75rem;
            padding: 1rem 1.5rem;
            border-top: 1px solid var(--border-color, #27272A);
        }
    `;
    document.head.appendChild(style);
})();

// ============================================
// Navbar: Collapsible folder toggles
// ============================================

(function () {
    'use strict';
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.folder-toggle').forEach(function (toggle) {
            toggle.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                var targetId = toggle.getAttribute('data-target');
                var submenu = document.getElementById(targetId);
                if (!submenu) return;
                var isOpen = submenu.classList.contains('show');
                submenu.classList.toggle('show', !isOpen);
                toggle.classList.toggle('open', !isOpen);
            });
        });

    });
})();

