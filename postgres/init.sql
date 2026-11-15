-- Accounts payable ledger. The dynamic user Vault mints inherits ap_read and nothing else.
-- Amounts live here. Policy text lives in the corpus, not in these tables.

CREATE ROLE ap_read NOLOGIN;

CREATE TABLE vendors (
    id int PRIMARY KEY,
    name text NOT NULL,
    status text NOT NULL
);

CREATE TABLE purchase_orders (
    id int PRIMARY KEY,
    vendor_id int NOT NULL REFERENCES vendors (id),
    description text NOT NULL,
    amount numeric NOT NULL,
    remaining numeric NOT NULL
);

CREATE TABLE invoices (
    id int PRIMARY KEY,
    vendor_id int NOT NULL REFERENCES vendors (id),
    po_id int NOT NULL REFERENCES purchase_orders (id),
    amount numeric NOT NULL
);

INSERT INTO vendors (id, name, status) VALUES
    (1, 'northwind', 'approved'),
    (2, 'contoso', 'blocked');

INSERT INTO purchase_orders (id, vendor_id, description, amount, remaining) VALUES
    (900, 1, 'packing supplies', 10000, 10000),
    (901, 2, 'consulting', 5000, 5000);

INSERT INTO invoices (id, vendor_id, po_id, amount) VALUES
    (4411, 1, 900, 9800),
    (4412, 1, 900, 12000),
    (4413, 2, 901, 1000);

GRANT CONNECT ON DATABASE payables TO ap_read;
GRANT USAGE ON SCHEMA public TO ap_read;
GRANT SELECT ON TABLE vendors, purchase_orders, invoices TO ap_read;

CREATE USER vault WITH PASSWORD 'vault-lab-password' CREATEROLE;
GRANT ap_read TO vault WITH ADMIN OPTION;
