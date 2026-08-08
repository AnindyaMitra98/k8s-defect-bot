locals {
  # ARM instance families are materially cheaper, so the AMI architecture follows
  # the instance type rather than being another thing to keep in sync.
  architecture = can(regex("^[a-z][0-9]+g[a-z]*\\.", var.instance_type)) ? "arm64" : "amd64"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-${local.architecture}-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "this" {
  key_name   = "${var.name_prefix}-key"
  public_key = tls_private_key.ssh.public_key_openssh
}

resource "local_sensitive_file" "ssh_key" {
  content         = tls_private_key.ssh.private_key_pem
  filename        = "${path.module}/.ssh/${var.name_prefix}.pem"
  file_permission = "0600"
}

resource "aws_instance" "node" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.node.id]
  key_name                    = aws_key_pair.this.key_name
  associate_public_ip_address = true

  user_data = templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
    k3s_version = var.k3s_version
    node_name   = var.name_prefix
  })

  # Changing the bootstrap means the node must be rebuilt -- k3s installs once.
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []

    content {
      market_type = "spot"

      spot_options {
        # one-time + terminate: an interrupted node is simply gone, and the next
        # apply builds a new one. Nothing here is worth persisting.
        spot_instance_type             = "one-time"
        instance_interruption_behavior = "terminate"
      }
    }
  }

  tags = {
    Name = "${var.name_prefix}-k3s"
  }
}
