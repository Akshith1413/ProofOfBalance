resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = format("%.2f", var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert at 80% of actual spend, and when the month is forecast to exceed 100%.
  dynamic "notification" {
    for_each = var.budget_alert_email == "" ? {} : {
      ACTUAL     = 80
      FORECASTED = 100
    }
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.key
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
