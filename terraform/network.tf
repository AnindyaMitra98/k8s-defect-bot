# A minimal public VPC. Deliberately no NAT gateway: at ~$32/month it would cost
# several times the instance it serves. The node sits in a public subnet with a
# public IP and reaches the internet through the internet gateway, which is free.

data "aws_availability_zones" "available" {
  state = "available"
}

data "http" "my_ip" {
  count = var.allowed_cidr == null ? 1 : 0
  url   = "https://checkip.amazonaws.com"
}

locals {
  allowed_cidr = var.allowed_cidr != null ? var.allowed_cidr : "${chomp(data.http.my_ip[0].response_body)}/32"
}

resource "aws_vpc" "this" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = var.name_prefix
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = var.name_prefix
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.name_prefix}-public"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name = "${var.name_prefix}-public"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "node" {
  name        = "${var.name_prefix}-node"
  description = "k3s node: SSH, dashboard, and optionally the Kubernetes API, from one CIDR only"
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${var.name_prefix}-node"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.node.id
  description       = "SSH, for the image build and chart install"
  cidr_ipv4         = local.allowed_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.node.id
  description       = "Traefik ingress serving the dashboard"
  cidr_ipv4         = local.allowed_cidr
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "kubernetes_api" {
  count = var.expose_kubernetes_api ? 1 : 0

  security_group_id = aws_security_group.node.id
  description       = "Kubernetes API, so kubectl works from your workstation"
  cidr_ipv4         = local.allowed_cidr
  from_port         = 6443
  to_port           = 6443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.node.id
  description       = "Package installs, k3s and Helm downloads, container image pulls"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
