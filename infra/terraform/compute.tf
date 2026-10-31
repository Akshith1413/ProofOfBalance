data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "aws_key_pair" "this" {
  key_name   = "${var.name}-key"
  public_key = var.ssh_public_key
}

# Allocated before the instance so its address can be baked into the k3s
# certificate (--tls-san); otherwise kubectl from a laptop rejects the API cert.
resource "aws_eip" "node" {
  domain     = "vpc"
  tags       = { Name = "${var.name}-node" }
  depends_on = [aws_internet_gateway.this]
}

resource "aws_instance" "node" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.node.id]
  key_name                    = aws_key_pair.this.key_name
  user_data_replace_on_change = true

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    public_ip   = aws_eip.node.public_ip
    k3s_channel = var.k3s_channel
    k3s_version = var.k3s_version
  })

  # Burstable (t*) types: standard credits, so a runaway process gets
  # throttled instead of billed. Other families have no credits.
  dynamic "credit_specification" {
    for_each = startswith(var.instance_type, "t") ? [1] : []
    content {
      cpu_credits = "standard"
    }
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
    tags                  = { Name = "${var.name}-root" }
  }

  tags = { Name = "${var.name}-k3s" }

  lifecycle {
    # A newer Ubuntu AMI must not silently replace the running cluster.
    ignore_changes = [ami]
  }
}

resource "aws_eip_association" "node" {
  instance_id   = aws_instance.node.id
  allocation_id = aws_eip.node.id
}

# Lets scripts/teardown.sh --pause stop the instance (no compute charges)
# while keeping the disk, the IP and everything else.
resource "aws_ec2_instance_state" "node" {
  instance_id = aws_instance.node.id
  state       = var.instance_state
}
