mod other;

/// alpha is not a call
pub fn alpha(n: u32) -> u32 {
    n + 1
}

pub fn beta() -> u32 {
    alpha(1)
}

pub fn gamma(items: &[u32]) -> u32 {
    let mut n = 0;
    items.iter().for_each(|i| {
        n += alpha(*i);
    });
    let f = alpha;
    let s = "alpha";
    f(2) + s.len() as u32
}

pub fn helper(x: u32) -> u32 {
    x
}

pub fn uses_helper() -> u32 {
    helper(1) + other::helper(2)
}

pub trait Speak {
    fn say(&self) -> &'static str;
}

pub struct Dog;
impl Speak for Dog {
    fn say(&self) -> &'static str {
        "woof"
    }
}

pub struct Cat;
impl Speak for Cat {
    fn say(&self) -> &'static str {
        "meow"
    }
}

pub fn talk(animal: &dyn Speak) -> &'static str {
    animal.say()
}

pub struct Buf {
    pub n: u32,
}
impl Buf {
    pub fn push(&mut self, x: u32) {
        self.n += x;
    }
}

pub struct Bag {
    pub n: u32,
}
impl Bag {
    pub fn push(&mut self, x: u32) {
        self.n += x;
    }
}

pub fn push_buf(b: &mut Buf) {
    b.push(1);
}

pub fn push_bag(b: &mut Bag) {
    b.push(2);
}
