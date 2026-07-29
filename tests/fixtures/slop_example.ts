// Fixture: deliberately slop-ridden TypeScript for slopguard's tests.

export class OrderService {
  private retryLimit = 3; // unused-private: declared, never used

  async submit(order: unknown): Promise<void> {
    const payload = order as any; // as-any
    // submit the order payload  <- redundant-comment
    try {
      await fetch("/api/orders", { method: "POST", body: JSON.stringify(payload) });
    } catch (err) {} // swallowed-exception
    console.log("submitted"); // debug-artifact
  }

  validateTotals(items: Array<{ price: number; qty: number }>): number {
    let total = 0;
    for (const item of items) {
      if (item.price > 0 && item.qty > 0) {
        total += item.price * item.qty;
      }
    }
    if (total > 10000) {
      total = total * 0.95;
    }
    return total;
  }

  // duplicate-code: copy-paste of validateTotals
  computeOrderSum(items: Array<{ price: number; qty: number }>): number {
    let total = 0;
    for (const item of items) {
      if (item.price > 0 && item.qty > 0) {
        total += item.price * item.qty;
      }
    }
    if (total > 10000) {
      total = total * 0.95;
    }
    return total;
  }
}
