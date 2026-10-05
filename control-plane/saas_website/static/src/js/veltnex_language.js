/* Shared local catalog for native checkout/configuration interactions. */
(function () {
    'use strict';
    var catalog;
    window.saasText = function (source) {
        if (!document.documentElement.lang.startsWith('ar')) return source;
        if (!catalog) {
            try { catalog = JSON.parse(document.getElementById('veltnex-language-catalog').textContent); }
            catch (error) { return source; }
        }
        return catalog[source] || source;
    };
})();
