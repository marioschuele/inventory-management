<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <div v-if="lastOrder" class="success-banner" role="status">
        <div class="success-text">
          <strong>{{ t('restocking.successTitle', { orderNumber: lastOrder.order_number }) }}</strong>
          <span>{{ t('restocking.successDetail', { days: lastOrder.lead_time_days, date: formatDate(lastOrder.expected_delivery) }) }}</span>
        </div>
        <router-link to="/orders" class="success-link">{{ t('restocking.viewOrders') }}</router-link>
      </div>

      <div v-if="submitError" class="error" role="alert">{{ submitError }}</div>

      <div v-if="recommendations.length === 0" class="card empty-state">
        {{ t('restocking.noRecommendations') }}
      </div>

      <template v-else>
        <div class="card budget-card">
          <div class="card-header">
            <h3 class="card-title">{{ t('restocking.budgetTitle') }}</h3>
            <span class="card-hint">{{ t('restocking.budgetHint') }}</span>
          </div>

          <div class="budget-layout">
            <div class="slider-block">
              <input
                v-model.number="budget"
                type="range"
                class="budget-slider"
                min="0"
                :max="budgetMax"
                :step="BUDGET_STEP"
                :style="{ '--fill': sliderFillPercent + '%' }"
                :aria-label="t('restocking.budgetSlider')"
              />
              <div class="slider-scale">
                <span>{{ formatCurrency(0) }}</span>
                <span>{{ formatCurrency(budgetMax) }}</span>
              </div>
            </div>

            <div class="budget-stats">
              <div class="budget-stat">
                <div class="stat-label">{{ t('restocking.budget') }}</div>
                <div class="budget-value">{{ formatCurrency(budget) }}</div>
              </div>
              <div class="budget-stat allocated">
                <div class="stat-label">{{ t('restocking.allocated') }}</div>
                <div class="budget-value">{{ formatCurrency(allocatedTotal) }}</div>
              </div>
              <div class="budget-stat remaining">
                <div class="stat-label">{{ t('restocking.remaining') }}</div>
                <div class="budget-value">{{ formatCurrency(remainingBudget) }}</div>
              </div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h3 class="card-title">{{ t('restocking.recommendations') }} ({{ recommendations.length }})</h3>
            <span class="card-hint">
              {{ t('restocking.selectedSummary', { selected: selectedItems.length, funded: fundedCount }) }}
            </span>
          </div>

          <div class="table-container">
            <table class="restock-table">
              <thead>
                <tr>
                  <th class="col-select"></th>
                  <th class="col-item">{{ t('restocking.table.item') }}</th>
                  <th class="col-category">{{ t('restocking.table.category') }}</th>
                  <th class="col-num">{{ t('restocking.table.onHandForecast') }}</th>
                  <th class="col-qty">{{ t('restocking.table.orderQty') }}</th>
                  <th class="col-num">{{ t('restocking.table.unitCost') }}</th>
                  <th class="col-num">{{ t('restocking.table.lineTotal') }}</th>
                  <th class="col-lead">{{ t('restocking.table.leadTime') }}</th>
                  <th class="col-trend">{{ t('restocking.table.trend') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in allocation"
                  :key="row.sku"
                  :class="{ unfunded: !row.funded, excluded: row.funded && !isSelected(row) }"
                >
                  <td class="col-select">
                    <input
                      type="checkbox"
                      class="row-checkbox"
                      :checked="isSelected(row)"
                      :disabled="!row.funded || submitting"
                      :aria-label="t('restocking.selectItem', { name: translateProductName(row.name) })"
                      @change="toggleSelected(row.sku)"
                    />
                  </td>
                  <td class="col-item">
                    <div class="item-sku">{{ row.sku }}</div>
                    <div class="item-name">{{ translateProductName(row.name) }}</div>
                  </td>
                  <td class="col-category">{{ translateCategory(row.category) }}</td>
                  <td class="col-num">
                    {{ row.quantity_on_hand }} / {{ row.forecasted_demand }}
                    <!-- Units on earlier submitted orders are already deducted from the shortfall -->
                    <div v-if="row.quantity_on_order > 0" class="on-order">
                      {{ t('restocking.onOrder', { qty: row.quantity_on_order }) }}
                    </div>
                  </td>
                  <td class="col-qty">
                    <span v-if="!row.funded" class="badge not-funded">{{ t('restocking.notFunded') }}</span>
                    <template v-else>
                      <strong :class="{ partial: row.partial }">{{ row.quantity }}</strong>
                      <span class="qty-meta">{{ t('restocking.ofShortfall', { count: row.shortfall }) }}</span>
                    </template>
                  </td>
                  <td class="col-num">{{ formatCurrency(row.unit_cost) }}</td>
                  <td class="col-num">
                    <strong v-if="row.funded">{{ formatCurrency(row.lineTotal) }}</strong>
                    <span v-else class="muted">-</span>
                  </td>
                  <td class="col-lead">{{ t('restocking.leadTimeDays', { days: row.lead_time_days }) }}</td>
                  <td class="col-trend">
                    <span :class="['badge', row.trend]">{{ t(`trends.${row.trend}`) }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="order-footer">
            <div class="order-total">
              <span class="stat-label">{{ t('restocking.allocated') }}</span>
              <strong>{{ formatCurrency(allocatedTotal) }}</strong>
            </div>
            <button
              type="button"
              class="btn-primary"
              :disabled="!canSubmit"
              @click="placeOrder"
            >
              {{ submitting ? t('restocking.placingOrder') : t('restocking.placeOrder') }}
            </button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'

// Slider granularity. The slider max is rounded UP to a multiple of this so the
// full recommended spend is always reachable.
const BUDGET_STEP = 1000

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, currentLocale, translateProductName } = useI18n()

    const currencySymbol = computed(() => {
      return currentCurrency.value === 'JPY' ? '¥' : '$'
    })

    const loading = ref(true)
    const error = ref(null)
    const recommendations = ref([])
    const budget = ref(null)

    // SKUs the user has unticked. Tracking exclusions (instead of selections)
    // means every funded row is checked by default, including rows that become
    // funded later when the budget slider is moved up.
    const excludedSkus = ref(new Set())

    const submitting = ref(false)
    const submitError = ref(null)
    const lastOrder = ref(null)

    // Total cost to cover every shortfall, used to size the slider.
    const totalRecommendedCost = computed(() => {
      return recommendations.value.reduce((sum, rec) => sum + rec.line_cost, 0)
    })

    const budgetMax = computed(() => {
      const roundedUp = Math.ceil(totalRecommendedCost.value / BUDGET_STEP) * BUDGET_STEP
      return Math.max(BUDGET_STEP, roundedUp)
    })

    const sliderFillPercent = computed(() => {
      if (!budgetMax.value) return 0
      return Math.min(100, ((budget.value || 0) / budgetMax.value) * 100)
    })

    // Greedy budget allocation over the priority-ranked list (server returns it
    // highest priority first). Walk the list in order; each item receives as many
    // units as it needs (its shortfall) or as many as the remaining budget can
    // buy, whichever is smaller, and that spend is deducted before moving on.
    // The walk STOPS at the first item that can't be fully funded: that item gets
    // a partial quantity and everything below it is "Not funded". This keeps strict
    // priority order instead of scattering leftover dollars as 1-unit lines on
    // cheaper, lower-priority items.
    //
    // Money is handled in integer cents to avoid floating-point drift (e.g.
    // floor(9.9999999 / 10) = 0); this also guarantees the client total never
    // exceeds the budget the server validates against.
    //
    // The allocation intentionally ignores the checkboxes: unticking an item
    // does not free its money for other items (see selectedItems below).
    const allocation = computed(() => {
      let remainingCents = Math.round((budget.value || 0) * 100)
      let exhausted = false

      return recommendations.value.map(rec => {
        const unitCents = Math.round(rec.unit_cost * 100)
        const affordable = unitCents > 0 ? Math.floor(remainingCents / unitCents) : rec.shortfall
        const quantity = exhausted ? 0 : Math.max(0, Math.min(rec.shortfall, affordable))
        const lineCents = quantity * unitCents
        remainingCents -= lineCents
        // First item not fully covered ends the walk for all lower-priority items
        if (quantity < rec.shortfall) exhausted = true

        return {
          ...rec,
          quantity,
          lineCents,
          lineTotal: lineCents / 100,
          funded: quantity > 0,
          partial: quantity > 0 && quantity < rec.shortfall
        }
      })
    })

    const isSelected = (row) => row.funded && !excludedSkus.value.has(row.sku)

    const toggleSelected = (sku) => {
      // Replace the Set so the change is picked up by all dependent computeds
      const next = new Set(excludedSkus.value)
      if (next.has(sku)) {
        next.delete(sku)
      } else {
        next.add(sku)
      }
      excludedSkus.value = next
    }

    // Checkboxes only filter what gets submitted and what counts as "Allocated".
    // An unticked item keeps its slot in the greedy allocation above, so its cost
    // is not redistributed to lower-priority items; it simply shows up as extra
    // "Remaining" budget.
    const selectedItems = computed(() => allocation.value.filter(isSelected))

    const fundedCount = computed(() => allocation.value.filter(row => row.funded).length)

    const allocatedCents = computed(() => {
      return selectedItems.value.reduce((sum, row) => sum + row.lineCents, 0)
    })

    const allocatedTotal = computed(() => allocatedCents.value / 100)

    const remainingBudget = computed(() => {
      return (Math.round((budget.value || 0) * 100) - allocatedCents.value) / 100
    })

    const canSubmit = computed(() => selectedItems.value.length > 0 && !submitting.value)

    // showSpinner=false is used after placing an order so the page (and the
    // success banner) stays visible while the list refreshes in the background.
    const loadRecommendations = async (showSpinner = true) => {
      try {
        if (showSpinner) {
          loading.value = true
          error.value = null
        }
        recommendations.value = await api.getRestockRecommendations()

        // Default the budget to 50% of the slider range on first load; on later
        // reloads keep the user's choice but clamp it to the (possibly new) max.
        if (budget.value === null) {
          budget.value = Math.round(budgetMax.value / 2 / BUDGET_STEP) * BUDGET_STEP
        } else if (budget.value > budgetMax.value) {
          budget.value = budgetMax.value
        }
      } catch (err) {
        console.error('Failed to load restocking recommendations:', err)
        if (showSpinner) {
          error.value = t('restocking.loadError')
        }
      } finally {
        if (showSpinner) {
          loading.value = false
        }
      }
    }

    const placeOrder = async () => {
      if (!canSubmit.value) return

      submitting.value = true
      submitError.value = null
      lastOrder.value = null

      try {
        const order = await api.createRestockOrder({
          items: selectedItems.value.map(row => ({ sku: row.sku, quantity: row.quantity })),
          budget: budget.value
        })
        lastOrder.value = order
        excludedSkus.value = new Set()
        await loadRecommendations(false)
      } catch (err) {
        console.error('Failed to place restocking order:', err)
        // FastAPI returns a string detail for our 400s; 422 validation errors
        // return an array, in which case fall back to the generic message.
        const detail = err.response?.data?.detail
        submitError.value = typeof detail === 'string' && detail ? detail : t('restocking.submitError')
      } finally {
        submitting.value = false
      }
    }

    const formatCurrency = (value) => {
      const amount = Number(value) || 0
      return `${currencySymbol.value}${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    }

    const formatDate = (dateString) => {
      const date = new Date(dateString)
      if (isNaN(date.getTime())) return '-'
      const locale = currentLocale.value === 'ja' ? 'ja-JP' : 'en-US'
      return date.toLocaleDateString(locale, {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
      })
    }

    const translateCategory = (category) => {
      const categoryMap = {
        'Circuit Boards': t('categories.circuitBoards'),
        'Sensors': t('categories.sensors'),
        'Actuators': t('categories.actuators'),
        'Controllers': t('categories.controllers'),
        'Power Supplies': t('categories.powerSupplies')
      }
      return categoryMap[category] || category
    }

    onMounted(() => loadRecommendations())

    return {
      t,
      BUDGET_STEP,
      loading,
      error,
      recommendations,
      budget,
      budgetMax,
      sliderFillPercent,
      allocation,
      isSelected,
      toggleSelected,
      selectedItems,
      fundedCount,
      allocatedTotal,
      remainingBudget,
      canSubmit,
      submitting,
      submitError,
      lastOrder,
      placeOrder,
      formatCurrency,
      formatDate,
      translateCategory,
      translateProductName
    }
  }
}
</script>

<style scoped>
.card-hint {
  font-size: 0.813rem;
  color: #64748b;
}

.empty-state {
  text-align: center;
  color: #64748b;
  padding: 2.5rem 1.25rem;
}

/* Success banner */
.success-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  color: #065f46;
  padding: 1rem 1.25rem;
  border-radius: 8px;
  margin-bottom: 1.25rem;
  font-size: 0.938rem;
}

.success-text {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.success-link {
  color: #047857;
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
}

.success-link:hover {
  text-decoration: underline;
}

/* Budget card */
.budget-layout {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(420px, 1.4fr);
  gap: 2rem;
  align-items: center;
}

@media (max-width: 960px) {
  .budget-layout {
    grid-template-columns: 1fr;
  }
}

.slider-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 0.5rem;
  font-size: 0.75rem;
  color: #64748b;
}

.budget-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 8px;
  border-radius: 4px;
  /* --fill is set inline so the track left of the thumb is highlighted */
  background: linear-gradient(
    to right,
    #2563eb 0%,
    #2563eb var(--fill),
    #e2e8f0 var(--fill),
    #e2e8f0 100%
  );
  outline: none;
  cursor: pointer;
}

.budget-slider:focus-visible {
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.25);
}

.budget-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #ffffff;
  border: 2px solid #2563eb;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.2);
  cursor: pointer;
}

.budget-slider::-moz-range-thumb {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #ffffff;
  border: 2px solid #2563eb;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.2);
  cursor: pointer;
}

.budget-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
}

.budget-stat {
  padding: 0.875rem 1rem;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
}

.budget-stat .stat-label {
  margin-bottom: 0.375rem;
  font-size: 0.75rem;
}

.budget-value {
  font-size: 1.375rem;
  font-weight: 700;
  color: #0f172a;
  letter-spacing: -0.025em;
}

.budget-stat.allocated .budget-value {
  color: #2563eb;
}

.budget-stat.remaining .budget-value {
  color: #059669;
}

/* Recommendations table */
.restock-table {
  table-layout: fixed;
  width: 100%;
  min-width: 1000px;
}

.col-select {
  width: 44px;
}

.col-item {
  width: 240px;
}

.col-category {
  width: 130px;
}

.col-num {
  width: 130px;
}

.col-qty {
  width: 130px;
}

.col-lead {
  width: 100px;
}

.col-trend {
  width: 120px;
}

.row-checkbox {
  width: 16px;
  height: 16px;
  accent-color: #2563eb;
  cursor: pointer;
}

.row-checkbox:disabled {
  cursor: not-allowed;
}

.item-sku {
  font-weight: 600;
  color: #0f172a;
}

.item-name {
  font-size: 0.813rem;
  color: #64748b;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qty-meta {
  margin-left: 0.375rem;
  font-size: 0.75rem;
  color: #64748b;
}

/* Partially funded: budget ran out before the full shortfall was covered */
.partial {
  color: #b45309;
}

.muted {
  color: #94a3b8;
}

.badge.not-funded {
  background: #f1f5f9;
  color: #64748b;
}

.on-order {
  font-size: 0.75rem;
  color: #2563eb;
  margin-top: 2px;
}

tr.unfunded td {
  color: #94a3b8;
}

tr.unfunded .item-sku,
tr.unfunded .item-name {
  color: #94a3b8;
}

tr.unfunded .badge:not(.not-funded) {
  opacity: 0.5;
}

tr.excluded td {
  color: #94a3b8;
}

tr.excluded .col-num strong,
tr.excluded .col-qty strong {
  text-decoration: line-through;
}

/* Footer with order total and submit button */
.order-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 1.5rem;
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid #e2e8f0;
}

.order-total {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
}

.order-total .stat-label {
  margin-bottom: 0;
}

.order-total strong {
  font-size: 1.25rem;
  color: #0f172a;
}

.btn-primary {
  padding: 0.625rem 1.5rem;
  background: #2563eb;
  border: 1px solid #2563eb;
  border-radius: 8px;
  color: #ffffff;
  font-weight: 600;
  font-size: 0.875rem;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s ease;
}

.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
  border-color: #1d4ed8;
}

.btn-primary:disabled {
  background: #cbd5e1;
  border-color: #cbd5e1;
  cursor: not-allowed;
}
</style>
