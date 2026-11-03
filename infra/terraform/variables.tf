variable "region" {
  description = "A region outside the US: Binance's API refuses US addresses. Tokyo is where Binance's matching engine runs."
  type        = string
  default     = "ap-northeast-1"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "ai-trader"
}

variable "instance_type" {
  description = "Graviton (arm64). t4g.medium (4 GB) fits the app, Redis and the research labs' pandas panels."
  type        = string
  default     = "t4g.medium"
}

variable "data_volume_gb" {
  description = "The EBS volume for backend/data (SQLite files, recorder, research cache). 4 GB in use on 2026-11-03."
  type        = number
  default     = 50
}

variable "db_instance_class" {
  description = "RDS Postgres class. db.t4g.micro (1 GB) is enough: the app holds one connection and the data is a few GB."
  type        = string
  default     = "db.t4g.micro"
}

variable "domain" {
  description = "The hostname the dashboard is served on (Caddy obtains the TLS certificate for it)."
  type        = string
}

variable "route53_zone_id" {
  description = "If the domain's DNS is in Route 53, its hosted zone id: the A record is managed here. Empty: create the A record yourself from the elastic_ip output."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "owner/name of the GitHub repository whose Actions may deploy (OIDC trust)."
  type        = string
  default     = "freemile2ru/trading-bot"
}

variable "ssh_cidr" {
  description = "Leave empty: the instance is reached with SSM Session Manager, no SSH port is opened. Set a CIDR to open 22 to it."
  type        = string
  default     = ""
}
