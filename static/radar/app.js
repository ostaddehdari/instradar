(() => {
  const body = document.body;
  const toggle = document.getElementById('menu-toggle');
  const close = document.getElementById('sidebar-close');
  const overlay = document.getElementById('sidebar-overlay');
  const setOpen = (open) => {
    body.classList.toggle('sidebar-open', open);
    toggle?.setAttribute('aria-expanded', String(open));
    overlay?.setAttribute('aria-hidden', String(!open));
  };
  toggle?.addEventListener('click', () => setOpen(!body.classList.contains('sidebar-open')));
  close?.addEventListener('click', () => setOpen(false));
  overlay?.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', event => event.key === 'Escape' && setOpen(false));
  document.querySelectorAll('.sidebar a').forEach(link => link.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 991.98px)').matches) setOpen(false);
  }));
  window.addEventListener('resize', () => {
    if (!window.matchMedia('(max-width: 991.98px)').matches) setOpen(false);
  });

  document.querySelectorAll('[data-telegram-picker]').forEach(picker => {
    const account = picker.querySelector('.telegram-account-select');
    const choices = picker.querySelectorAll('[data-telegram-account]');
    const empty = picker.querySelector('[data-destination-empty]');
    if (!account || !choices.length) return;
    const filterDestinations = () => {
      let shown = 0;
      choices.forEach(choice => {
        const visible = Boolean(account.value) && choice.dataset.telegramAccount === account.value;
        choice.classList.toggle('is-hidden', !visible);
        if (visible) shown += 1;
        const input = choice.querySelector('input');
        if (!visible && input) input.checked = false;
      });
      if (empty) {
        empty.classList.toggle('is-hidden', shown > 0);
        empty.textContent = account.value
          ? 'برای این حساب مقصد مدیریتی قابل ارسال ثبت نشده؛ فهرست کانال‌ها را تازه‌سازی کنید.'
          : 'حساب تلگرام را انتخاب کنید.';
      }
    };
    account.addEventListener('change', filterDestinations);
    filterDestinations();
  });

  const publishToggle = document.getElementById('publish-toggle');
  const telegramFields = document.querySelector('.telegram-fields');
  const toggleTelegramFields = () => {
    telegramFields?.classList.toggle('is-hidden', !publishToggle.checked);
    telegramFields?.querySelectorAll('select, textarea, input').forEach(field => {
      if (field !== publishToggle) field.disabled = !publishToggle.checked;
    });
    if (!publishToggle.checked) telegramFields?.querySelectorAll('input[type="checkbox"]')
      .forEach(input => { input.checked = false; });
  };
  publishToggle?.addEventListener('change', toggleTelegramFields);
  if (publishToggle) toggleTelegramFields();

  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));

  window.addEventListener('load', () => {
    if (window.jalaliDatepicker) window.jalaliDatepicker.startWatch({
      separatorChar: '/', changeMonthRotateYear: true, showTodayBtn: true,
      showEmptyBtn: true, autoHide: true
    });
  });
  document.querySelectorAll('[data-check-all]').forEach(master => master.addEventListener('change', () => {
    document.querySelectorAll('[data-check-item]').forEach(item => { item.checked = master.checked; });
  }));

  const csrfToken = document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
    document.cookie.split('; ').find(item => item.startsWith('csrftoken='))?.split('=').slice(1).join('=');
  const postOrder = async (url, key, values) => {
    const body = new URLSearchParams();
    values.forEach(value => body.append(`${key}[]`, value));
    const response = await fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': decodeURIComponent(csrfToken || ''),
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}, body});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };
  const board = document.querySelector('[data-job-sort-url]');
  if (board) {
    let dragged = null;
    board.querySelectorAll('.unified-job-card').forEach(card => {
      card.addEventListener('dragstart', event => {
        if (event.target.closest('.job-stages')) { event.preventDefault(); return; }
        dragged = card; card.classList.add('dragging'); board.classList.add('dragging');
      });
      card.addEventListener('dragend', async () => {
        card.classList.remove('dragging'); board.classList.remove('dragging'); dragged = null;
        try { await postOrder(board.dataset.jobSortUrl, 'job_ids',
          [...board.querySelectorAll('.unified-job-card')].map(item => item.dataset.jobId)); }
        catch (_) { window.alert('ذخیرهٔ اولویت جاب‌ها ناموفق بود؛ صفحه را تازه کنید.'); }
      });
      card.addEventListener('dragover', event => {
        if (!dragged) return;
        event.preventDefault();
        const box = card.getBoundingClientRect();
        board.insertBefore(dragged, event.clientY < box.top + box.height / 2 ? card : card.nextSibling);
      });
    });
  }
  document.querySelectorAll('[data-stage-sort-url]').forEach(list => {
    let dragged = null;
    const saveOrder = async () => {
      const state = list.closest('.card')?.querySelector('[data-sort-state]');
      if (state) state.textContent = 'در حال ذخیره…';
      try {
        await postOrder(list.dataset.stageSortUrl, 'stages',
          [...list.querySelectorAll('[data-stage]')].map(node => node.dataset.stage));
        if (state) state.textContent = 'ترتیب ذخیره شد';
      } catch (_) {
        if (state) state.textContent = 'ذخیره ناموفق بود';
        window.alert('ذخیرهٔ ترتیب زیرکارها ناموفق بود؛ صفحه را تازه کنید.');
      }
    };
    list.querySelectorAll('[data-stage]').forEach(item => {
      item.tabIndex = 0;
      item.addEventListener('dragstart', event => {
        event.stopPropagation(); dragged = item; item.classList.add('dragging');
      });
      item.addEventListener('dragover', event => {
        event.preventDefault(); event.stopPropagation();
        if (dragged && dragged !== item) {
          const box = item.getBoundingClientRect();
          const vertical = list.classList.contains('subtask-list');
          const before = vertical ? event.clientY < box.top + box.height / 2
                                  : event.clientX > box.left + box.width / 2;
          list.insertBefore(dragged, before ? item : item.nextSibling);
        }
      });
      item.addEventListener('dragend', async event => {
        event.stopPropagation(); item.classList.remove('dragging'); dragged = null;
        await saveOrder();
      });
      item.addEventListener('keydown', async event => {
        if (!event.altKey || !['ArrowUp', 'ArrowDown', 'ArrowRight', 'ArrowLeft'].includes(event.key)) return;
        event.preventDefault();
        const previous = item.previousElementSibling;
        const next = item.nextElementSibling;
        if (['ArrowUp', 'ArrowRight'].includes(event.key) && previous) list.insertBefore(item, previous);
        if (['ArrowDown', 'ArrowLeft'].includes(event.key) && next) list.insertBefore(next, item);
        await saveOrder(); item.focus();
      });
    });
  });

  document.querySelectorAll('[data-check-all]').forEach(toggleAll => {
    const target = document.querySelector(toggleAll.dataset.checkAll);
    if (!target) return;
    toggleAll.addEventListener('change', () => {
      target.querySelectorAll('input[type="checkbox"]:not(:disabled)').forEach(input => {
        input.checked = toggleAll.checked;
      });
    });
  });

  const faToEn = value => value.replace(/[۰-۹]/g, char => '۰۱۲۳۴۵۶۷۸۹'.indexOf(char));
  document.querySelectorAll('table:not([data-server-table]):not([data-no-tools])').forEach(table => {
    const tbody = table.tBodies[0];
    const headers = [...(table.tHead?.rows[0]?.cells || [])];
    if (!tbody || !headers.length) return;
    const rows = () => [...tbody.rows].filter(row => !row.querySelector('.empty'));
    if (rows().length < 2 && !table.hasAttribute('data-table-tools')) return;
    const host = table.closest('.table-responsive,.table-wrap') || table;
    const toolbar = document.createElement('div');
    toolbar.className = 'table-tools';
    toolbar.innerHTML = '<label><i class="fa-solid fa-magnifying-glass"></i><input type="search" placeholder="فیلتر همین جدول"></label><small></small>';
    host.parentNode.insertBefore(toolbar, host);
    const input = toolbar.querySelector('input');
    const count = toolbar.querySelector('small');
    const updateCount = () => {
      const visible = rows().filter(row => !row.hidden).length;
      count.textContent = `${visible} ردیف`;
    };
    input.addEventListener('input', () => {
      const needle = input.value.trim().toLocaleLowerCase('fa');
      rows().forEach(row => { row.hidden = Boolean(needle) && !row.textContent.toLocaleLowerCase('fa').includes(needle); });
      updateCount();
    });
    headers.forEach((header, index) => {
      if (!header.textContent.trim()) return;
      header.classList.add('client-sortable');
      header.title = 'مرتب‌سازی این ستون';
      header.addEventListener('click', event => {
        if (event.target.closest('a,button,input')) return;
        const ascending = header.dataset.sortDirection !== 'asc';
        headers.forEach(item => delete item.dataset.sortDirection);
        header.dataset.sortDirection = ascending ? 'asc' : 'desc';
        const value = row => faToEn(row.cells[index]?.textContent.trim() || '').replace(/[٬,]/g, '');
        rows().sort((left, right) => {
          const a = value(left), b = value(right);
          const an = Number(a), bn = Number(b);
          const result = a !== '' && b !== '' && Number.isFinite(an) && Number.isFinite(bn)
            ? an - bn : a.localeCompare(b, 'fa', {numeric: true, sensitivity: 'base'});
          return ascending ? result : -result;
        }).forEach(row => tbody.appendChild(row));
      });
    });
    updateCount();
  });
})();
