import asyncio
from playwright.async_api import Page
from .models import DetectedForm, FormField
from typing import Optional, List, Dict
import logging
import re

logger = logging.getLogger(__name__)


async def pre_scan_scroll(page: Page):
    """Smoothly scrolls the page down and back up to trigger lazy-loaded elements.
    Uses incremental scrolling (like a human reading) rather than instant jumps.
    Works with both Page and Frame objects."""
    # Frame objects don't have viewport_size — fall back gracefully.
    viewport = getattr(page, "viewport_size", None) or {"height": 800}
    scroll_step = viewport["height"] * 0.6  # Scroll ~60% of viewport at a time

    # Get total scrollable height
    total_height = await page.evaluate("document.body.scrollHeight")

    current = 0
    while current < total_height:
        current += scroll_step
        await page.evaluate(f"window.scrollTo({{ top: {current}, behavior: 'instant' }})")
        await asyncio.sleep(0.15)
        total_height = await page.evaluate("document.body.scrollHeight")

    await page.evaluate("window.scrollTo({ top: 0, behavior: 'instant' })")
    await asyncio.sleep(0.2)


def _clean_label(text: str) -> str:
    """Clean extracted label text: remove extra whitespace, line breaks, etc."""
    text = re.sub(r'\s+', ' ', text.strip())
    return text


async def _extract_label_for_element(page: Page, el, id_attr: str, name_attr: str, aria_label: str) -> str:
    """Extract the most meaningful label for a form element using multiple strategies:
    1. aria-label attribute
    2. <label for="..."> association
    3. Closest parent label wrapping the element
    4. Preceding sibling text
    5. placeholder attribute
    6. Fall back to name/id attribute
    """
    # Strategy 1: aria-label
    if aria_label:
        return _clean_label(aria_label)

    # Strategy 2: <label for="id"> association
    if id_attr:
        label_el = await page.query_selector(f"label[for='{id_attr}']")
        if label_el:
            text = await label_el.inner_text()
            if text.strip():
                return _clean_label(text)

    # Strategy 3: Closest wrapping <label> (e.g., <label><input>...</label>)
    wrapping_label = await el.evaluate("""el => {
        let parent = el.closest('label');
        if (parent) {
            // Get the label's text content, excluding the input's own text
            let clone = parent.cloneNode(true);
            clone.querySelectorAll('input, select, textarea').forEach(c => c.remove());
            return clone.textContent.trim();
        }
        return '';
    }""")
    if wrapping_label:
        return _clean_label(wrapping_label)

    # Strategy 4: Previous sibling or parent text (common in table-based forms)
    sibling_text = await el.evaluate("""el => {
        // Check previous sibling
        let prev = el.previousElementSibling;
        if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN' || prev.tagName === 'DIV')) {
            return prev.textContent.trim();
        }
        // Check parent's previous sibling (table layouts: <td>Label</td><td><input></td>)
        let parentTd = el.closest('td, div.form-group, div.field');
        if (parentTd && parentTd.previousElementSibling) {
            return parentTd.previousElementSibling.textContent.trim();
        }
        return '';
    }""")
    if sibling_text:
        return _clean_label(sibling_text)

    # Strategy 5: Placeholder attribute
    placeholder = await el.get_attribute("placeholder") or ""
    if placeholder:
        return _clean_label(placeholder)

    # Strategy 6: Fall back to name or id
    fallback = name_attr or id_attr
    if fallback:
        # Convert snake_case/camelCase to readable: "first_name" -> "first name"
        readable = re.sub(r'[_-]', ' ', fallback)
        readable = re.sub(r'([a-z])([A-Z])', r'\1 \2', readable)
        return _clean_label(readable)

    return ""


async def _extract_radio_group_label(page: Page, el) -> str:
    """Extract the question label for a radio/checkbox group.
    
    Radio buttons are typically structured like:
        <div class="form-group">
            <label for="...">Question text? *</label>
            <div class="radio-group">
                <label><input type="radio" name="..." value="Yes"> Yes</label>
                <label><input type="radio" name="..." value="No"> No</label>
            </div>
        </div>
    
    We traverse upward to find the container, then look for a label
    that describes the entire group (not the individual radio option).
    """
    group_label = await el.evaluate("""el => {
        // Strategy 1: Find the radio-group/checkbox-group container,
        // then look for a preceding sibling label
        let radioGroup = el.closest('.radio-group, .checkbox-group, [role="radiogroup"], [role="group"]');
        if (radioGroup) {
            let prev = radioGroup.previousElementSibling;
            while (prev) {
                if (prev.tagName === 'LABEL' || prev.tagName === 'LEGEND' || prev.tagName === 'SPAN' || prev.tagName === 'P') {
                    return prev.textContent.trim();
                }
                prev = prev.previousElementSibling;
            }
        }

        // Strategy 2: Traverse up to form-group/fieldset and find a label/legend
        let container = el.closest('.form-group, .field-group, fieldset, .form-field, .form-row, .field');
        if (container) {
            let labels = container.querySelectorAll(':scope > label, :scope > legend, :scope > span, :scope > .form-group > label');
            for (let lbl of labels) {
                let hasInput = lbl.querySelector('input[type="radio"], input[type="checkbox"]');
                if (!hasInput && lbl.textContent.trim().length > 2) {
                    return lbl.textContent.trim();
                }
            }
        }

        // Strategy 3: Walk up through nested divs to find a label or question text
        let parent = el.parentElement;
        let depth = 0;
        while (parent && depth < 8) {
            // Check all labels in this container that don't wrap a radio/checkbox
            let labels = parent.querySelectorAll(':scope > label, :scope > legend, :scope > span.label');
            for (let lbl of labels) {
                let hasInput = lbl.querySelector('input[type="radio"], input[type="checkbox"]');
                if (!hasInput && lbl.textContent.trim().length > 3) {
                    return lbl.textContent.trim();
                }
            }
            // Greenhouse-specific: look for question text in parent's text content
            // excluding child input/label text
            let clone = parent.cloneNode(true);
            clone.querySelectorAll('input, .option-label, label:has(input)').forEach(c => c.remove());
            let cleanText = clone.textContent.trim();
            if (cleanText.length > 5 && /[a-zA-Z?]/.test(cleanText)) {
                return cleanText.substring(0, 200);
            }
            parent = parent.parentElement;
            depth++;
        }

        return '';
    }""")
    return _clean_label(group_label) if group_label else ""


async def detect_form(page: Page, container_selector: Optional[str] = None, skip_scroll: bool = False) -> DetectedForm:
    """Detect and analyze all form fields on the page or within a container.
    
    Performs a human-like pre-scan scroll to trigger lazy-loaded content,
    then extracts field metadata including labels, types, options, and
    required status.
    
    Radio buttons with the same name are consolidated into a single field
    with options (like a select dropdown), making it easier for the filler
    to choose the right value.
    """
    if not skip_scroll:
        await pre_scan_scroll(page)

    # Query all interactive form elements
    if container_selector:
        container = await page.query_selector(container_selector)
        if container:
            elements = await container.query_selector_all("input, select, textarea")
        else:
            logger.warning(f"Container selector '{container_selector}' not found. Falling back to whole page.")
            elements = await page.query_selector_all("input, select, textarea")
    else:
        elements = await page.query_selector_all("input, select, textarea")

    fields = []
    has_file_upload = False
    seen_selectors = set()  # Deduplicate
    radio_groups: Dict[str, dict] = {}  # name -> {label, options, required, selector}

    for el in elements:
        # Skip hidden inputs (except file inputs which are often hidden for styling)
        hidden_info = await el.evaluate("""el => {
            if (el.type === 'hidden') return {hidden: true, reason: 'type=hidden'};
            if (el.type === 'file') return {hidden: false, reason: 'file'};
            let style = window.getComputedStyle(el);
            if (style.display === 'none') return {hidden: true, reason: 'display:none'};
            // Allow visibility:hidden for radio/checkbox — Greenhouse uses CSS-overlay technique
            if (style.visibility === 'hidden' && el.type !== 'radio' && el.type !== 'checkbox')
                return {hidden: true, reason: 'visibility:hidden'};
            // offsetParent is null for position:fixed elements too — don't filter those
            if (el.offsetParent === null && style.position !== 'fixed' && style.position !== 'sticky')
                return {hidden: true, reason: 'offsetParent=null'};
            let rect = el.getBoundingClientRect();
            // Allow zero-size for radio/checkbox — they're often visually replaced with CSS
            if ((rect.width === 0 || rect.height === 0) && el.type !== 'radio' && el.type !== 'checkbox')
                return {hidden: true, reason: 'zero-size'};
            // Allow opacity:0 for radio/checkbox — Greenhouse and many ATS platforms
            // hide the native input and overlay custom-styled elements
            if (style.opacity === '0' && el.type !== 'radio' && el.type !== 'checkbox')
                return {hidden: true, reason: 'opacity:0'};
            return {hidden: false, reason: 'visible'};
        }""")
        if hidden_info.get("hidden"):
            name_or_id = await el.get_attribute("name") or await el.get_attribute("id") or "?"
            type_attr = await el.get_attribute("type") or "text"
            logger.debug(f"Filtered out: type={type_attr}, name={name_or_id}, reason={hidden_info['reason']}")
            continue

        tag_name = await el.evaluate("el => el.tagName.toLowerCase()")
        type_attr = await el.get_attribute("type") or "text"
        name_attr = await el.get_attribute("name") or ""
        id_attr = await el.get_attribute("id") or ""
        aria_label = await el.get_attribute("aria-label") or ""
        role_attr = (await el.get_attribute("role")) or ""

        # ── Skip combobox <input> — these are the search inputs INSIDE
        # react-select / custom dropdown widgets. The custom-dropdown scan
        # below picks up the wrapping .select__control and reports it as
        # field_type="select" with custom_widget=True. If we also report the
        # inner <input> as a text field, the filler would type into the search
        # box without ever opening the menu, never committing the choice.
        if tag_name == "input" and role_attr.lower() == "combobox":
            logger.debug(f"Skipping combobox input (id={id_attr}) — handled by custom-dropdown scan")
            continue

        # ── Special handling for radio buttons: consolidate into groups ──
        if type_attr == "radio" and name_attr:
            value_attr = await el.get_attribute("value") or ""
            if name_attr not in radio_groups:
                group_label = await _extract_radio_group_label(page, el)
                required_attr = await el.get_attribute("required")
                aria_required = await el.get_attribute("aria-required")
                required = (required_attr is not None) or (aria_required == "true") or ("*" in group_label)
                radio_groups[name_attr] = {
                    "label": group_label,
                    "options": [],
                    "values": [],
                    "required": required,
                    "selector": f"input[name='{name_attr}']",
                }
            if value_attr:
                radio_groups[name_attr]["values"].append(value_attr)
                # Get visible text label for this radio option
                visible_text = await el.evaluate("""el => {
                    // Check wrapping <label> text (excluding the input itself)
                    let lbl = el.closest('label');
                    if (lbl) {
                        let clone = lbl.cloneNode(true);
                        clone.querySelectorAll('input').forEach(c => c.remove());
                        let t = clone.textContent.trim();
                        if (t.length > 0) return t;
                    }
                    // Check next sibling text
                    let next = el.nextSibling;
                    if (next && next.nodeType === 3 && next.textContent.trim())
                        return next.textContent.trim();
                    if (next && next.nodeType === 1)
                        return next.textContent.trim();
                    return '';
                }""")
                radio_groups[name_attr]["options"].append(visible_text or value_attr)
            continue

        # ── Extract label using multi-strategy engine ──
        label_text = await _extract_label_for_element(page, el, id_attr, name_attr, aria_label)

        # Determine field_type from HTML type attribute / tag
        field_type = "text"
        if tag_name == "textarea":
            field_type = "textarea"
        elif tag_name == "select":
            field_type = "select"
        elif type_attr == "email":
            field_type = "email"
        elif type_attr in ("tel", "phone"):
            field_type = "phone"
        elif type_attr == "file":
            field_type = "file"
            has_file_upload = True
        elif type_attr == "checkbox":
            field_type = "checkbox"
        elif type_attr == "date":
            field_type = "date"
        elif type_attr == "url":
            field_type = "url"
        elif type_attr == "number":
            field_type = "text"  # Treat number inputs as text for filling

        # Check if required
        required_attr = await el.get_attribute("required")
        aria_required = await el.get_attribute("aria-required")
        required = (required_attr is not None) or (aria_required == "true") or ("*" in label_text)

        # Extract options for select elements
        options = None
        if field_type == "select":
            option_elements = await el.query_selector_all("option")
            options = []
            for opt in option_elements:
                text = await opt.inner_text()
                value = await opt.get_attribute("value")
                # Skip placeholder options (empty value or "Select...")
                if text.strip() and value != "":
                    options.append(text.strip())

        # Build the best selector (id > name > tag fallback)
        if id_attr:
            selector = f"#{id_attr}"
        elif name_attr:
            selector = f"{tag_name}[name='{name_attr}']"
        else:
            selector = tag_name

        # Deduplicate by selector
        if selector in seen_selectors:
            continue
        seen_selectors.add(selector)

        fields.append(FormField(
            selector=selector,
            field_type=field_type,
            label=label_text.strip(),
            required=required,
            options=options
        ))
        logger.debug(f"Detected field: type={field_type}, label='{label_text.strip()}', "
                     f"selector='{selector}', required={required}, options={options}")

    # ── Add consolidated radio groups as fields ──
    for name, group in radio_groups.items():
        fields.append(FormField(
            selector=f"input[name='{name}']",
            field_type="radio",
            label=group["label"] or name,
            required=group["required"],
            options=group["options"] if group["options"] else None,
            raw_values=group.get("values"),
        ))
        logger.debug(f"Detected radio group: name='{name}', label='{group['label']}', "
                     f"options={group['options']}, values={group.get('values')}, required={group['required']}")

    # ── Detect Greenhouse-style React-Select custom dropdowns ─────────────────
    # Greenhouse hides the real input (visibility:hidden) and renders a div with
    # class containing "select-shell", "select__control", or role="combobox".
    # These are common for gender, race/ethnicity, veteran status, etc.
    try:
        custom_dropdowns = await page.evaluate("""() => {
            // The clickable trigger lives inside .select-shell / .react-select__control
            const trigger_sel = (
                'div.select-shell-button, .select__control, .react-select__control, '
                + '[role="combobox"]:not(input):not(select)'
            );
            const triggers = document.querySelectorAll(trigger_sel);
            const seen = new Set();
            const results = [];
            for (const trigger of triggers) {
                if (seen.has(trigger)) continue;
                seen.add(trigger);
                const r = trigger.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;  // skip ghosts

                // BUG B fix: skip intl-tel-input's country picker. The phone
                // widget renders `<input role="combobox">` that we previously
                // mis-detected as the form's Country dropdown — then memory
                // recall wrote "United States" into the phone country picker
                // and the real Country field stayed empty. Anything inside
                // an .iti / .iti__country-list / .iti__selected-flag wrapper
                // is the phone widget, not a form field.
                if (trigger.closest('.iti, .iti__country-list, .iti__selected-flag, .iti--container')) {
                    continue;
                }
                // Also skip triggers whose accessible name is just a dial code
                // (some phone widgets don't wrap in .iti but render +N as the
                // selected value).
                const accName = (trigger.getAttribute('aria-label') || '').trim();
                if (/^\\+\\d{1,3}$/.test(accName)) continue;

                // Walk up to find the labelling container + label text
                let label = '';
                let labelEl = null;
                let p = trigger.parentElement;
                for (let i = 0; i < 6 && p; i++) {
                    // Look for a sibling label/legend/span.label
                    const cand = p.querySelector(
                        ':scope > label, :scope > legend, :scope > span.label, '
                        + ':scope > div > label, :scope > .label'
                    );
                    if (cand && !cand.contains(trigger)) {
                        const t = cand.textContent.trim();
                        if (t.length > 1 && t.length < 200) {
                            label = t;
                            labelEl = cand;
                            break;
                        }
                    }
                    p = p.parentElement;
                }

                // Build a stable CSS selector for the trigger
                let sel = '';
                if (trigger.id) sel = '#' + trigger.id;
                else if (labelEl && labelEl.htmlFor) sel = '#' + labelEl.htmlFor;
                else {
                    // unique-ish: parent id + nth descendant of class
                    let cls = (typeof trigger.className === 'string')
                        ? trigger.className.split(' ').filter(c => c).map(c => '.' + c).join('')
                        : '';
                    sel = trigger.tagName.toLowerCase() + cls;
                }

                // Try to read currently-displayed value (placeholder vs selection)
                const placeholderClasses = ['select__placeholder', 'react-select__placeholder',
                                           'select-shell-button-placeholder'];
                let hasSelection = true;
                for (const pc of placeholderClasses) {
                    if (trigger.querySelector('.' + pc)) { hasSelection = false; break; }
                }

                results.push({
                    label: label,
                    selector: sel,
                    has_selection: hasSelection,
                    required: /\\*/.test(label),
                });
            }
            return results;
        }""")
        for d in custom_dropdowns:
            sel = d['selector']
            if not sel or sel in seen_selectors:
                continue
            # Skip if label is missing AND no useful selector
            if not d['label']:
                continue
            seen_selectors.add(sel)
            fields.append(FormField(
                selector=sel,
                field_type="select",
                label=d['label'],
                required=d['required'],
                options=None,
                custom_widget=True,
            ))
            logger.debug(f"Detected custom dropdown: label='{d['label']}', "
                         f"selector='{sel}', required={d['required']}")
    except Exception as exc:
        logger.warning(f"Custom dropdown detection error (non-fatal): {exc}")

    # ── Detect form type ──
    url = page.url
    form_type = "UNKNOWN"
    easy_apply_btn = await page.query_selector("button:has-text('Easy Apply')")
    mailto_apply = await page.query_selector("a[href^='mailto:']")

    if "linkedin.com" in url and easy_apply_btn:
        form_type = "EASY_APPLY"
    elif mailto_apply:
        form_type = "EMAIL"
    elif await page.query_selector("form"):
        form_type = "EXTERNAL_FORM"

    # ── Detect multi-step ──
    steps = 1
    current_step = 1
    next_btn = await page.query_selector("button:has-text('Next')")
    if next_btn:
        steps = 2  # Placeholder for actual step detection

    # ── Detect captcha ──
    has_captcha = False
    captcha_type = None
    if await page.query_selector(".g-recaptcha[data-size='invisible']"):
        has_captcha = True
        captcha_type = "recaptcha_invisible"
    elif await page.evaluate("() => !!document.querySelector('.grecaptcha-badge') || !!document.querySelector('script[src*=\"render=\"]')"):
        has_captcha = True
        captcha_type = "recaptcha_v3"
    elif await page.query_selector(".g-recaptcha, iframe[src*='recaptcha']"):
        has_captcha = True
        captcha_type = "recaptcha_v2"
    elif await page.query_selector(".h-captcha"):
        has_captcha = True
        captcha_type = "hcaptcha"

    # ── Find submit selector ──
    submit_selector = "button[type='submit'], input[type='submit']"
    submit_btn = await page.query_selector("button:has-text('Submit'), button:has-text('Apply')")
    if submit_btn:
        btn_id = await submit_btn.get_attribute("id")
        if btn_id:
            submit_selector = f"#{btn_id}"

    logger.info(f"Form detection complete: {len(fields)} fields, type={form_type}, "
                f"captcha={has_captcha}, file_upload={has_file_upload}")

    return DetectedForm(
        form_type=form_type,
        fields=fields,
        steps=steps,
        current_step=current_step,
        has_captcha=has_captcha,
        captcha_type=captcha_type,
        has_file_upload=has_file_upload,
        submit_selector=submit_selector
    )
