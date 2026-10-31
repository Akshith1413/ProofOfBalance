# A small dedicated VPC instead of the default one: some accounts have no
# default VPC, and a dedicated one is removed cleanly by `terraform destroy`.

data "aws_ec2_instance_type_offerings" "available" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

locals {
  # First AZ (alphabetically) that actually offers the instance type.
  az = sort(data.aws_ec2_instance_type_offerings.available.locations)[0]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-igw" }

  lifecycle {
    # If the VPC is ever replaced, replace (detach) the gateway too. An in-place
    # move to the new VPC would keep it attached to the old one, and AWS refuses
    # to delete a VPC with an attached gateway: Terraform then waits forever.
    replace_triggered_by = [aws_vpc.this.id]
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 24 - tonumber(split("/", var.vpc_cidr)[1]), 1)
  availability_zone       = local.az
  map_public_ip_on_launch = false # the instance gets an Elastic IP instead
  tags                    = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "node" {
  name        = "${var.name}-node"
  description = "ProofOfBalance k3s node: SSH and k3s API only from my_ip; HTTP(S) open"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-node" }
}

locals {
  # Every address is an exact /32. Several are needed only when your network
  # load-balances across multiple public IPs (setup.sh detects all of them).
  admin_ips = toset(distinct(concat([var.my_ip], var.extra_admin_ips)))
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each          = local.admin_ips
  security_group_id = aws_security_group.node.id
  description       = "SSH from admin IP ${each.value}"
  cidr_ipv4         = "${each.value}/32"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_ingress_rule" "k3s_api" {
  for_each          = local.admin_ips
  security_group_id = aws_security_group.node.id
  description       = "Kubernetes API from admin IP ${each.value}"
  cidr_ipv4         = "${each.value}/32"
  ip_protocol       = "tcp"
  from_port         = 6443
  to_port           = 6443
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.node.id
  description       = "HTTP(S) from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.node.id
  description       = "All outbound (image pulls, k3s install, webhooks)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
