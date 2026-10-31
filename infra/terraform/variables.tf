variable "region" {
  description = "AWS region."
  type        = string
  default     = "ap-south-1"
}

variable "owner" {
  description = "Owner tag on every resource."
  type        = string
  default     = "Akshith1413"
}

variable "environment" {
  description = "Environment tag on every resource."
  type        = string
  default     = "demo"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "pob"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "name must be 2-21 lower-case letters, digits or dashes."
  }
}

variable "vpc_cidr" {
  description = "VPC address range. Must NOT overlap k3s's pod (10.42.0.0/16) or service (10.43.0.0/16) networks, or in-cluster routing breaks."
  type        = string
  default     = "10.20.0.0/16"

  validation {
    condition = (
      can(cidrhost(var.vpc_cidr, 0)) &&
      tonumber(split("/", var.vpc_cidr)[1]) >= 16 && tonumber(split("/", var.vpc_cidr)[1]) <= 24 &&
      !can(regex("^10\\.4[23]\\.", var.vpc_cidr))
    )
    error_message = "vpc_cidr must be a /16../24 that does not overlap k3s's 10.42.0.0/16 (pods) or 10.43.0.0/16 (services)."
  }
}

variable "my_ip" {
  description = "Your public IPv4 address. SSH (22) and the k3s API (6443) are only open to my_ip/32."
  type        = string

  validation {
    condition     = can(cidrhost("${var.my_ip}/32", 0)) && can(regex("^\\d+\\.\\d+\\.\\d+\\.\\d+$", var.my_ip))
    error_message = "my_ip must be a single IPv4 address such as 203.0.113.7 (no /32)."
  }
}

variable "extra_admin_ips" {
  description = "More single IPv4 addresses allowed on 22/6443 (networks that load-balance across several public IPs)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for ip in var.extra_admin_ips : can(regex("^\\d+\\.\\d+\\.\\d+\\.\\d+$", ip)) && can(cidrhost("${ip}/32", 0))])
    error_message = "extra_admin_ips must be single IPv4 addresses (no /32)."
  }
}

variable "ssh_public_key" {
  description = "OpenSSH public key for the ubuntu user."
  type        = string

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) ", var.ssh_public_key))
    error_message = "ssh_public_key must be an OpenSSH public key (ssh-ed25519 / ssh-rsa / ecdsa)."
  }
}

variable "instance_type" {
  description = "Needs >= 4 GB for k3s + Prometheus + Grafana + Argo + the app. AWS Free-plan accounts only allow Free-Tier types; scripts/setup.sh then picks m7i-flex.large (8 GB) or c7i-flex.large (4 GB)."
  type        = string
  default     = "t3.medium"
}

variable "root_volume_gb" {
  description = "Root EBS volume size (gp3)."
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_gb >= 20 && var.root_volume_gb <= 200
    error_message = "root_volume_gb must be between 20 and 200."
  }
}

variable "instance_state" {
  description = "running, or stopped to pause compute costs without destroying anything."
  type        = string
  default     = "running"

  validation {
    condition     = contains(["running", "stopped"], var.instance_state)
    error_message = "instance_state must be running or stopped."
  }
}

variable "k3s_channel" {
  description = "k3s release channel (stable, latest) or an exact version via k3s_version."
  type        = string
  default     = "stable"
}

variable "k3s_version" {
  description = "Exact k3s version (e.g. v1.36.5+k3s1). Empty = use k3s_channel."
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to assume the CI roles."
  type        = string
  default     = "Akshith1413/ProofOfBalance"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repo))
    error_message = "github_repo must look like owner/name."
  }
}

variable "create_github_oidc_provider" {
  description = "An AWS account can hold only one GitHub OIDC provider. Set false if it already exists (setup.sh detects this)."
  type        = bool
  default     = true
}

variable "state_bucket" {
  description = "Name of the Terraform state bucket, so the CI roles can read/write state. Empty = no state permissions."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly AWS budget in USD."
  type        = number
  default     = 10

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be positive."
  }
}

variable "budget_alert_email" {
  description = "Email for budget alerts at 80% (actual) and 100% (forecast). Empty = budget without email alerts."
  type        = string
  default     = ""
  sensitive   = true # plan output is public on a public repo's PRs

  validation {
    condition     = var.budget_alert_email == "" || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be empty or a valid email address."
  }
}
