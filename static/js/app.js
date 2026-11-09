// 业务登记协同系统 · 前端交互
(function () {
  'use strict';

  // 0. 轻提示
  function toast(msg, ms) {
    let wrap = document.querySelector('.toast-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'toast-wrap';
      document.body.appendChild(wrap);
    }
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () { el.remove(); }, ms || 2600);
  }

  // 0.1 待改密锁定态：点菜单不做无反馈的跳转，直接提示原因
  const lockUrl = document.body.getAttribute('data-lock-url');
  if (document.body.getAttribute('data-locked') === '1') {
    document.addEventListener('click', function (e) {
      const link = e.target.closest('a');
      if (!link) return;
      const href = link.getAttribute('href') || '';
      // 放行：改密页本身、退出登录、页内锚点
      if (!href || href.charAt(0) === '#' || href.indexOf(lockUrl) === 0 ||
          href.indexOf('/logout') === 0) {
        return;
      }
      e.preventDefault();
      toast('请先设置新密码，之后才能使用其他功能');
    }, true);
  }

  window.wbToast = toast;

  // 1. 带 data-confirm 的表单，提交前二次确认
  document.addEventListener('submit', function (e) {
    const form = e.target;
    const msg = form.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
    }
  });

  // 2. 状态筛选 chip
  document.querySelectorAll('.chip input[type=checkbox]').forEach(function (input) {
    const sync = function () {
      input.closest('.chip').classList.toggle('on', input.checked);
    };
    sync();
    input.addEventListener('change', sync);
  });

  // 3. 表格行点击跳转
  document.querySelectorAll('tr[data-href]').forEach(function (tr) {
    tr.addEventListener('click', function (e) {
      if (e.target.closest('a, button, input, form, .ck-cell')) return;
      window.location = tr.getAttribute('data-href');
    });
  });

  // 4. 金额试算（登记表单）
  const calcHost = document.getElementById('calcPanel');
  if (calcHost) {
    const $ = function (id) { return document.getElementById(id); };

    const num = function (id) {
      const el = $(id);
      if (!el) return 0;
      const v = String(el.value || '').replace(/[,，¥￥元\s]/g, '');
      const n = parseFloat(v);
      return isFinite(n) ? n : 0;
    };

    const fmt = function (n) {
      return (Math.round(n * 100) / 100).toLocaleString('zh-CN', {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      });
    };

    const render = function () {
      const amt = num('biz_amount');
      const rate = num('fee_rate');
      const retention = num('channel_commission');   // 留存利润（K 列合计）
      const overnight = num('fee_overnight');        // 资金过夜费（计入应收）
      const prepaid = num('prepaid_amount');
      const paid = num('received_amount');   // 已收金额（只读，由收款登记累计）

      // 贸易费默认 = 业务金额 × 费率；手工改过就尊重手工值（四舍五入差异）
      const feeEl = $('trade_fee');
      const formulaFee = Math.round(amt * rate / 100 * 100) / 100;
      let tradeFee = formulaFee;
      if (feeManual && feeEl) {
        const typed = parseFloat(String(feeEl.value || '').replace(/[,，¥￥元\s]/g, ''));
        tradeFee = isFinite(typed) ? Math.round(typed * 100) / 100 : 0;
      } else if (feeEl) {
        // 没手工改过：跟着公式走（新单自动填出，改金额/费率时同步刷新）
        feeEl.value = formulaFee.toFixed(2);
      }
      const receivable = Math.round((tradeFee + retention + overnight) * 100) / 100;
      const unpaid = Math.round((receivable - prepaid - paid) * 100) / 100;

      // 费用明细四项合计（仅登记，不参与贸易费/待收计算）
      let feeTotal = 0;
      document.querySelectorAll('.fee-item').forEach(function (el) {
        const n = parseFloat(String(el.value || '').replace(/[,，¥￥元\s]/g, ''));
        if (isFinite(n)) feeTotal += n;
      });
      feeTotal = Math.round(feeTotal * 100) / 100;

      const set = function (id, text, negative) {
        const el = $(id);
        if (!el) return;
        el.textContent = text + ' 元';
        el.classList.toggle('neg', !!negative);
      };
      set('calcTradeFee', fmt(tradeFee));
      set('calcRetention', fmt(retention));
      set('calcOvernight', fmt(overnight));
      set('calcReceivable', fmt(receivable));
      set('calcFee', fmt(feeTotal));
      set('calcUnpaid', fmt(unpaid), unpaid < 0);
      set('calcUnpaidTop', fmt(unpaid), unpaid < 0);

      // 手改过就说明差多少，避免改了金额忘了回头看贸易费
      const hint = $('tradeFeeHint');
      if (hint) {
        const gap = Math.round((tradeFee - formulaFee) * 100) / 100;
        if (feeManual && Math.abs(gap) >= 0.005) {
          hint.textContent = '已手工修正：公式值 ' + fmt(formulaFee) +
            ' 元，当前差 ' + (gap > 0 ? '+' : '') + fmt(gap) + ' 元';
          hint.classList.add('warn');
        } else {
          hint.textContent = '业务金额 × 费率，自动算出；可手工改（如四舍五入取整）';
          hint.classList.remove('warn');
        }
      }
    };

    // false = 跟公式；true = 用输入框里的手工值
    let feeManual = false;
    const feeInput = $('trade_fee');
    if (feeInput) {
      // 编辑旧单：库里存的贸易费与公式不一致，说明当时是手工定的，保持不动
      const stored = parseFloat(String(feeInput.value || '').replace(/[,，¥￥元\s]/g, ''));
      const formulaNow = Math.round(num('biz_amount') * num('fee_rate') / 100 * 100) / 100;
      if (isFinite(stored) && Math.abs(stored - formulaNow) >= 0.005) feeManual = true;
      feeInput.addEventListener('input', function () { feeManual = true; render(); });
    }
    const resetBtn = document.getElementById('tradeFeeReset');
    if (resetBtn) {
      resetBtn.addEventListener('click', function () {
        feeManual = false;
        render();
        if (feeInput) feeInput.focus();
      });
    }

    ['biz_amount', 'fee_rate', 'channel_commission', 'prepaid_amount'].forEach(function (id) {
      const el = $(id);
      if (el) el.addEventListener('input', render);
    });
    document.querySelectorAll('.fee-item').forEach(function (el) {
      el.addEventListener('input', render);
    });
    render();
  }

  // 5. 人员权限：切换角色时套用该角色的默认权限
  const roleSelect = document.getElementById('roleSelect');
  const roleDefaults = window.ROLE_DEFAULTS || {};
  if (roleSelect && roleSelect.form) {
    const permBoxes = roleSelect.form.querySelectorAll('.perm-items input[type=checkbox]');
    roleSelect.addEventListener('change', function () {
      const defaults = roleDefaults[roleSelect.value] || [];
      permBoxes.forEach(function (box) {
        box.checked = defaults.indexOf(box.value) !== -1;
      });
    });
  }

  // 6. 弹窗（驳回 / 付款 / 编辑人员）
  window.openDialog = function (id) {
    const dlg = document.getElementById(id);
    if (dlg && typeof dlg.showModal === 'function') dlg.showModal();
  };
  document.querySelectorAll('[data-close-dialog]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const dlg = btn.closest('dialog');
      if (dlg) dlg.close();
    });
  });

  // 7. 导入页拖拽
  const drop = document.getElementById('dropZone');
  if (drop) {
    const fileInput = document.getElementById('fileInput');
    drop.addEventListener('click', function () { fileInput.click(); });
    drop.addEventListener('dragover', function (e) {
      e.preventDefault();
      drop.classList.add('dragging');
    });
    drop.addEventListener('dragleave', function () { drop.classList.remove('dragging'); });
    drop.addEventListener('drop', function (e) {
      e.preventDefault();
      drop.classList.remove('dragging');
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        drop.querySelector('.dz-name').textContent = e.dataTransfer.files[0].name;
      }
    });
    fileInput.addEventListener('change', function () {
      if (fileInput.files.length) {
        drop.querySelector('.dz-name').textContent = fileInput.files[0].name;
      }
    });
  }

  // 8. 列表页批量选择：全选 / 反选 / 批量操作联动（业务列表、客户档案共用）
  (function () {
    const form = document.getElementById('batchForm');
    if (!form) return;

    const boxes = Array.prototype.slice.call(document.querySelectorAll('.row-check'));
    const allBoxes = Array.prototype.slice.call(document.querySelectorAll('.js-check-all'));
    const selectable = boxes.filter(function (b) { return !b.disabled; });
    const countEl = document.getElementById('selCount');
    const clearBtn = document.getElementById('clearSel');
    const invertBtn = document.getElementById('invertSel');
    const bar = document.getElementById('bulkbar');
    const actionBtns = Array.prototype.slice.call(
      document.querySelectorAll('[data-bulk-action]')
    );

    function selectedCount() {
      return boxes.filter(function (b) { return b.checked; }).length;
    }

    function refresh() {
      const n = selectedCount();
      const on = n > 0;

      if (countEl) countEl.textContent = n;
      actionBtns.forEach(function (btn) {
        btn.disabled = !on;
        const tpl = btn.getAttribute('data-confirm-tpl');
        if (tpl) btn.setAttribute('data-confirm', tpl.replace(/\{n\}/g, String(n)));
      });
      if (clearBtn) clearBtn.disabled = !on;
      if (bar) bar.classList.toggle('has-sel', on);

      allBoxes.forEach(function (box) {
        box.checked = selectable.length > 0 && n === selectable.length;
        box.indeterminate = n > 0 && n < selectable.length;
      });

      boxes.forEach(function (box) {
        const host = box.closest('tr, .rec-card-item');
        if (host) host.classList.toggle('sel', box.checked);
      });
    }

    boxes.forEach(function (box) {
      box.addEventListener('change', refresh);
    });

    allBoxes.forEach(function (box) {
      box.addEventListener('change', function () {
        selectable.forEach(function (b) { b.checked = box.checked; });
        refresh();
      });
    });

    if (invertBtn) {
      invertBtn.addEventListener('click', function () {
        selectable.forEach(function (b) { b.checked = !b.checked; });
        refresh();
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        boxes.forEach(function (b) { b.checked = false; });
        refresh();
      });
    }

    // 一个表单多个动作按钮时，点击哪个就把动作写进隐藏字段
    actionBtns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        const hidden = form.querySelector('input[name=action]');
        if (hidden) hidden.value = btn.getAttribute('data-bulk-action') || '';
      });
    });

    // Shift + 点击连选，方便整段圈定
    let lastIndex = null;
    boxes.forEach(function (box, idx) {
      box.addEventListener('click', function (e) {
        if (box.disabled) return;
        if (!e.shiftKey || lastIndex === null) { lastIndex = idx; return; }
        const from = Math.min(lastIndex, idx);
        const to = Math.max(lastIndex, idx);
        for (let i = from; i <= to; i++) {
          if (!boxes[i].disabled) boxes[i].checked = box.checked;
        }
        lastIndex = idx;
        refresh();
      });
    });

    form.addEventListener('submit', function (e) {
      if (selectedCount() === 0) {
        e.preventDefault();
        e.stopPropagation();
        if (window.wbToast) window.wbToast('请先勾选要处理的记录');
      }
    });

    refresh();
  })();
})();
