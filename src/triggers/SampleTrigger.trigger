trigger SampleTrigger on Contact (after delete, after insert, after undelete, 
after update) {

    SampleClass myClass = new SampleClass();
    myClass.fillInFirstName(Trigger.new);
}