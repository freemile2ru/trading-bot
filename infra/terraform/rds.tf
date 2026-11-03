# PostgreSQL 16 on RDS, private to the instance's VPC: the production database (storage/engine.py maps each of the
# app's SQLite files to a schema here). db.t4g.micro: the app keeps ONE connection per process (see _Shared), so
# the micro's ~80-connection ceiling is never near. Automated backups 7 days, deletion protection on.

resource "aws_subnet" "db" {
  count = 2

  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 3, count.index + 2) # 10.42.0.64/27, 10.42.0.96/27
  availability_zone = data.aws_availability_zones.this.names[count.index]
  tags              = { Name = "${var.name}-db-${count.index}" }
}

resource "aws_db_subnet_group" "db" {
  name       = "${var.name}-db"
  subnet_ids = aws_subnet.db[*].id
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Postgres from the app instance only"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.name}-db" }
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_instance" "db" {
  identifier     = "${var.name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "aitrader"
  username = "aitrader"
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.db.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period      = 7
  backup_window                = "17:00-18:00" # 02:00–03:00 JST, after the US close
  maintenance_window           = "sun:18:00-sun:19:00"
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-db-final"
  apply_immediately            = false
  performance_insights_enabled = false

  tags = { Name = "${var.name}-db" }
}

# The connection string the app reads (deploy/deploy.sh adds it to app.env). The password is in Terraform state —
# the state bucket is private and encrypted; rotate with `terraform taint random_password.db` and an apply.
resource "aws_ssm_parameter" "database_url" {
  name  = "/ai-trader/database-url"
  type  = "SecureString"
  value = "postgresql://${aws_db_instance.db.username}:${random_password.db.result}@${aws_db_instance.db.address}:${aws_db_instance.db.port}/${aws_db_instance.db.db_name}?sslmode=require"
}

output "database_endpoint" {
  value = aws_db_instance.db.address
}
